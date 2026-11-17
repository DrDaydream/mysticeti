// Copyright (c) Mysten Labs, Inc.
// SPDX-License-Identifier: Apache-2.0

//! Bounded, local adversarial proposal construction and network scheduling.
//!
//! This module never changes committee membership, consensus thresholds or decisions.
//! It signs only this node's configured Byzantine identity. Its clock starts with Core,
//! so attack windows on different machines can differ by their startup skew.

use crate::{
    block_store::BlockStore,
    committee::Committee,
    consensus::LeaderStatus,
    crypto::Signer,
    data::Data,
    network::NetworkMessage,
    runtime,
    types::{AuthorityIndex, BlockReference, RoundNumber, StatementBlock},
    wal::MAX_ENTRY_SIZE,
};
use parking_lot::Mutex;
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet, HashSet},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Weak,
    },
    time::Duration,
};
use tokio::sync::{mpsc, Notify};

const MAX_DAG_VISITS: usize = 65_536;
const MAX_OBSERVED_OUTCOMES: usize = 1_024;

#[derive(Clone, Copy, Debug, Default, Deserialize, Serialize, Eq, PartialEq)]
pub enum AdversaryMode {
    #[default]
    A0,
    A1,
    A2,
    A3,
    A4,
    A5,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct AdversaryConfig {
    pub mode: AdversaryMode,
    pub byzantine_authorities: Vec<AuthorityIndex>,
    pub seed: u64,
    pub start_after_ms: u64,
    pub duration_ms: u64,
    pub delay_ms: u64,
    pub late_delay_ms: u64,
    pub max_pending_messages: usize,
    pub max_pending_bytes: usize,
    pub max_extra_bytes_per_round: usize,
    pub target_authority: Option<AuthorityIndex>,
    pub target_round: Option<RoundNumber>,
}

impl Default for AdversaryConfig {
    fn default() -> Self {
        Self {
            mode: AdversaryMode::A0,
            byzantine_authorities: vec![],
            seed: 1,
            start_after_ms: 0,
            duration_ms: 60_000,
            delay_ms: 200,
            late_delay_ms: 1_000,
            max_pending_messages: 4_096,
            max_pending_bytes: 67_108_864,
            max_extra_bytes_per_round: 8_388_608,
            target_authority: None,
            target_round: None,
        }
    }
}

impl AdversaryConfig {
    pub fn validate(
        &self,
        committee: &Committee,
        wave_length: RoundNumber,
        number_of_leaders: usize,
        pipeline: bool,
    ) -> Result<(), String> {
        if self.mode == AdversaryMode::A0 {
            return if self.byzantine_authorities.is_empty() {
                Ok(())
            } else {
                Err("A0 requires an empty Byzantine set".into())
            };
        }
        if wave_length < 3 || number_of_leaders == 0 || number_of_leaders > committee.len() {
            return Err(
                "adversary requires the actual wave length >= 3 and 1..=committee size leaders"
                    .into(),
            );
        }
        let mut identities = BTreeSet::new();
        let mut bad_stake = 0u128;
        for authority in &self.byzantine_authorities {
            if !committee.known_authority(*authority) || !identities.insert(*authority) {
                return Err(
                    "Byzantine authorities must be unique, existing committee identities".into(),
                );
            }
            bad_stake += committee.get_stake(*authority).unwrap() as u128;
        }
        let total_stake: u128 = committee
            .authorities()
            .map(|a| committee.get_stake(a).unwrap() as u128)
            .sum();
        if bad_stake * 3 >= total_stake {
            return Err("configured Byzantine stake must be strictly below one third".into());
        }
        if (self.mode == AdversaryMode::A0) != identities.is_empty() {
            return Err(
                "A0 requires an empty Byzantine set; A1-A5 require a nonempty fixed set".into(),
            );
        }
        if self.duration_ms == 0
            || self.delay_ms == 0
            || self.late_delay_ms == 0
            || self.max_pending_messages == 0
            || self.max_pending_bytes == 0
            || self.max_extra_bytes_per_round == 0
        {
            return Err(
                "attack duration, delays and memory/extra-byte budgets must be positive".into(),
            );
        }
        if self.start_after_ms.checked_add(self.duration_ms).is_none() {
            return Err("attack start plus duration overflows milliseconds".into());
        }
        if self.late_delay_ms < self.delay_ms {
            return Err("late_delay_ms must be at least delay_ms".into());
        }
        if let Some(authority) = self.target_authority {
            if !identities.contains(&authority) {
                return Err("target_authority must belong to the fixed Byzantine set".into());
            }
        }
        if let Some(round) = self.target_round {
            if round == 0 || round.checked_add(wave_length).is_none() {
                return Err(
                    "target_round must be positive and leave room for a complete wave".into(),
                );
            }
            if !identities.iter().any(|a| {
                self.target_authority.map_or(true, |wanted| wanted == *a)
                    && scheduled_leader(
                        committee,
                        wave_length,
                        number_of_leaders,
                        pipeline,
                        *a,
                        round,
                    )
            }) {
                return Err("target_round does not elect a configured Byzantine target with these leader/pipeline options".into());
            }
        }
        Ok(())
    }
}

fn scheduled_leader(
    committee: &Committee,
    wave_length: RoundNumber,
    leaders: usize,
    pipeline: bool,
    authority: AuthorityIndex,
    round: RoundNumber,
) -> bool {
    if round == 0 || (!pipeline && round % wave_length != 0) {
        return false;
    }
    // BaseCommitter uses elect_leader(round + leader_offset), with one round_offset
    // per pipeline stage. Reducing before addition also handles large input rounds.
    let size = committee.len() as u64;
    (0..leaders).any(|offset| committee.elect_leader(round % size + offset as u64) == authority)
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct LeaderSlot {
    round: RoundNumber,
    authority: AuthorityIndex,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ObservedOutcome {
    Commit(BlockReference),
    Skip,
    Undecided,
}

#[derive(Default)]
struct State {
    outcomes: BTreeMap<LeaderSlot, ObservedOutcome>,
    earliest_undecided: Option<LeaderSlot>,
    last_target: Option<LeaderSlot>,
    retired_outcomes: Option<LeaderSlot>,
    pending_messages: usize,
    pending_bytes: usize,
}

pub(crate) struct Adversary {
    config: AdversaryConfig,
    authority: AuthorityIndex,
    committee: Arc<Committee>,
    block_store: BlockStore,
    wave_length: RoundNumber,
    number_of_leaders: usize,
    pipeline: bool,
    started: runtime::TimeInstant,
    state: Mutex<State>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Support {
    Found(BlockReference),
    Absent,
    Incomplete,
}

impl Adversary {
    pub(crate) fn new(
        config: AdversaryConfig,
        authority: AuthorityIndex,
        committee: Arc<Committee>,
        block_store: BlockStore,
        wave_length: RoundNumber,
        number_of_leaders: usize,
        pipeline: bool,
    ) -> Arc<Self> {
        config
            .validate(&committee, wave_length, number_of_leaders, pipeline)
            .unwrap_or_else(|error| panic!("Invalid adversary configuration: {error}"));
        assert!(
            committee.known_authority(authority),
            "Adversary local identity is not in committee"
        );
        if config.mode != AdversaryMode::A0 {
            tracing::info!(event = "adversary_start", mode = ?config.mode, seed = config.seed,
                authority, byzantine = ?config.byzantine_authorities, wave_length, number_of_leaders,
                pipeline, start_after_ms = config.start_after_ms, duration_ms = config.duration_ms,
                target_authority = ?config.target_authority, target_round = ?config.target_round,
                "Bounded adversary uses local monotonic Core-start time");
        }
        Arc::new(Self {
            config,
            authority,
            committee,
            block_store,
            wave_length,
            number_of_leaders,
            pipeline,
            started: runtime::TimeInstant::now(),
            state: Mutex::new(State::default()),
        })
    }

    fn is_byzantine(&self, authority: AuthorityIndex) -> bool {
        self.config.byzantine_authorities.contains(&authority)
    }

    fn remaining_at(&self, elapsed: Duration) -> Option<Duration> {
        let start = Duration::from_millis(self.config.start_after_ms);
        let end = Duration::from_millis(self.config.start_after_ms + self.config.duration_ms);
        (self.config.mode != AdversaryMode::A0 && elapsed >= start && elapsed < end)
            .then(|| end - elapsed)
    }

    fn eligible_target(&self, round: RoundNumber) -> Option<LeaderSlot> {
        self.config
            .byzantine_authorities
            .iter()
            .copied()
            .filter(|a| {
                self.config
                    .target_authority
                    .map_or(true, |wanted| wanted == *a)
            })
            .filter(|a| {
                scheduled_leader(
                    &self.committee,
                    self.wave_length,
                    self.number_of_leaders,
                    self.pipeline,
                    *a,
                    round,
                )
            })
            .min()
            .map(|authority| LeaderSlot { round, authority })
    }

    fn next_target(&self, start: RoundNumber) -> Option<LeaderSlot> {
        let step = if self.pipeline { 1 } else { self.wave_length };
        let mut round = start.max(1);
        if !self.pipeline && round % step != 0 {
            round = round.checked_add(step - round % step)?;
        }
        // Round-robin repeats within committee.len() wave steps, even without a pipeline.
        for _ in 0..=self.committee.len() {
            if let Some(target) = self.eligible_target(round) {
                let decided = matches!(
                    self.state.lock().outcomes.get(&target),
                    Some(ObservedOutcome::Commit(_) | ObservedOutcome::Skip)
                );
                if !decided {
                    return Some(target);
                }
            }
            round = round.checked_add(step)?;
        }
        None
    }

    fn target_for_round(&self, round: RoundNumber) -> Option<LeaderSlot> {
        if let Some(fixed) = self.config.target_round {
            return self.eligible_target(fixed);
        }
        if matches!(self.config.mode, AdversaryMode::A4 | AdversaryMode::A5) {
            let earliest = self.state.lock().earliest_undecided;
            if let Some(earliest) = earliest {
                if self.is_byzantine(earliest.authority)
                    && self
                        .config
                        .target_authority
                        .map_or(true, |wanted| wanted == earliest.authority)
                {
                    return Some(earliest);
                }
                // An honest unresolved leader cannot be equivocated. Prepare a future
                // Byzantine anchor while reporting the honest unresolved leader separately.
                let start = earliest
                    .round
                    .saturating_add(self.wave_length)
                    .max(round.saturating_sub(self.wave_length - 2));
                return self.next_target(start);
            }
        }
        // A proposal can simultaneously vote for a prior leader and occupy a new
        // leader slot. Preserve the active vote window; prioritize its last Vote round.
        for candidate in round.saturating_sub(self.wave_length - 2).max(1)..round {
            if let Some(target) = self.eligible_target(candidate) {
                return Some(target);
            }
        }
        if let Some(target) = self.eligible_target(round) {
            return Some(target);
        }
        if matches!(self.config.mode, AdversaryMode::A4 | AdversaryMode::A5) {
            self.next_target(round)
        } else {
            None
        }
    }

    pub(crate) fn observe_leaders<'a>(&self, leaders: impl Iterator<Item = &'a LeaderStatus>) {
        if self.config.mode == AdversaryMode::A0 {
            return;
        }
        let mut state = self.state.lock();
        let mut earliest = None;
        for leader in leaders.filter(|leader| leader.round() > 0) {
            let target = LeaderSlot {
                round: leader.round(),
                authority: leader.authority(),
            };
            let outcome = match leader {
                LeaderStatus::Commit(block) => ObservedOutcome::Commit(*block.reference()),
                LeaderStatus::Skip(..) => ObservedOutcome::Skip,
                LeaderStatus::Undecided(..) => {
                    if earliest.is_none() {
                        earliest = Some(target);
                    }
                    ObservedOutcome::Undecided
                }
            };
            let fixed = self.config.target_round == Some(target.round)
                && self
                    .config
                    .target_authority
                    .map_or(true, |a| a == target.authority);
            let retained = state.outcomes.contains_key(&target);
            let retired = state
                .retired_outcomes
                .map_or(false, |oldest| target <= oldest);
            if state.outcomes.get(&target) != Some(&outcome)
                && (retained || !retired || fixed || earliest == Some(target))
            {
                tracing::info!(event = "adversary_outcome", mode = ?self.config.mode, seed = self.config.seed,
                    authority = self.authority, target = ?target, outcome = ?outcome,
                    committed_digest = ?match outcome { ObservedOutcome::Commit(reference) => Some(reference.digest), _ => None },
                    "Observed final local consensus result after direct and indirect rules");
                state.outcomes.insert(target, outcome);
                // Bound the registry even during a large first observation after recovery.
                while state.outcomes.len() > MAX_OBSERVED_OUTCOMES {
                    let evict = state.outcomes.keys().copied().find(|key| {
                        Some(*key) != earliest.or(state.earliest_undecided)
                            && !(self.config.target_round == Some(key.round)
                                && self
                                    .config
                                    .target_authority
                                    .map_or(true, |a| a == key.authority))
                    });
                    let Some(key) = evict else {
                        break;
                    };
                    state.outcomes.remove(&key);
                    state.retired_outcomes =
                        Some(state.retired_outcomes.map_or(key, |old| old.max(key)));
                }
            }
        }
        if state.earliest_undecided != earliest {
            tracing::info!(event = "adversary_earliest_undecided", mode = ?self.config.mode, seed = self.config.seed,
                authority = self.authority, target = ?earliest, "Local unresolved prefix changed");
        }
        state.earliest_undecided = earliest;
    }

    /// Same ordered depth-first search as BaseCommitter::find_support. A missing
    /// earlier subtree is unknown, not permission to claim support from a later one.
    fn find_support(&self, target: LeaderSlot, from: &Data<StatementBlock>) -> Support {
        if from.round() < target.round {
            return Support::Absent;
        }
        if from.includes().len() > MAX_DAG_VISITS {
            return Support::Incomplete;
        }
        let mut stack: Vec<_> = from.includes().iter().rev().copied().collect();
        let mut visited = HashSet::new();
        while let Some(reference) = stack.pop() {
            if reference.round < target.round {
                continue;
            }
            if reference.author_round() == (target.authority, target.round) {
                return Support::Found(reference);
            }
            if !visited.insert(reference) {
                continue;
            }
            if visited.len() > MAX_DAG_VISITS {
                return Support::Incomplete;
            }
            let Some(parent) = self.block_store.get_block(reference) else {
                return Support::Incomplete;
            };
            if stack.len().saturating_add(parent.includes().len()) > MAX_DAG_VISITS {
                return Support::Incomplete;
            }
            stack.extend(parent.includes().iter().rev().copied());
        }
        Support::Absent
    }

    fn support_of(&self, target: LeaderSlot, block: &Data<StatementBlock>) -> Support {
        if block.author_round() == (target.authority, target.round) {
            Support::Found(*block.reference())
        } else {
            self.find_support(target, block)
        }
    }

    fn has_quorum(&self, references: &[BlockReference], round: RoundNumber) -> bool {
        let mut authors = BTreeSet::new();
        let stake = references
            .iter()
            .filter(|r| r.round == round && authors.insert(r.authority))
            .map(|r| self.committee.get_stake(r.authority).unwrap_or(0))
            .sum();
        self.committee.is_quorum(stake)
    }

    fn sorted_variants(
        &self,
        authority: AuthorityIndex,
        round: RoundNumber,
    ) -> Vec<Data<StatementBlock>> {
        let mut variants = self
            .block_store
            .get_blocks_at_authority_round(authority, round);
        variants.sort_unstable_by_key(|block| *block.reference());
        variants.dedup_by_key(|block| *block.reference());
        variants
    }

    fn proposal_fallback(&self, round: RoundNumber, target: Option<LeaderSlot>, reason: &str) {
        tracing::info!(event = "adversary_fallback", mode = ?self.config.mode, seed = self.config.seed,
            authority = self.authority, round, target = ?target, reason, "Proposal strategy fallback");
    }

    fn sign_parents(
        &self,
        normal: &Data<StatementBlock>,
        parents: Vec<BlockReference>,
        signer: &Signer,
    ) -> Data<StatementBlock> {
        Data::new(StatementBlock::new_with_signer(
            self.authority,
            normal.round(),
            parents,
            normal.statements().clone(),
            normal.meta_creation_time_ns(),
            normal.epoch_changed(),
            signer,
        ))
    }

    fn nondirected_parents(
        &self,
        normal: &Data<StatementBlock>,
    ) -> Result<Vec<BlockReference>, &'static str> {
        if normal.includes().first().map(|r| r.authority) != Some(self.authority) {
            return Err("normal proposal has no first self-parent");
        }
        let mut alternate = normal.includes().clone();
        alternate[1..].reverse();
        if alternate == *normal.includes() {
            // A palindromic tail can still be permuted without changing the self-parent.
            let distinct = (2..alternate.len()).find(|i| alternate[*i] != alternate[1]);
            if let Some(index) = distinct {
                alternate.swap(1, index);
            } else {
                return Err("not enough distinct non-self parents for a different digest");
            }
        }
        Ok(alternate)
    }

    fn directed_parents(
        &self,
        normal: &Data<StatementBlock>,
        target: LeaderSlot,
        desired: BlockReference,
    ) -> Result<Vec<BlockReference>, &'static str> {
        let first = normal
            .includes()
            .first()
            .ok_or("normal proposal has no self-parent")?;
        if first.authority != self.authority {
            return Err("first parent is not local author");
        }
        let mut own_candidates = self.sorted_variants(self.authority, first.round);
        // Favor the normal branch when it is compatible, then a stored equivocation.
        own_candidates.sort_by_key(|b| (*b.reference() != *first, *b.reference()));
        let self_parent = own_candidates
            .into_iter()
            .find(|b| {
                matches!(self.support_of(target, b),
            Support::Found(reference) if reference == desired)
            })
            .or_else(|| {
                self.sorted_variants(self.authority, first.round)
                    .into_iter()
                    .find(|b| self.support_of(target, b) == Support::Absent)
            })
            .ok_or("no locally stored self-parent compatible with the requested branch")?;

        let previous_round = normal
            .round()
            .checked_sub(1)
            .ok_or("genesis cannot equivocate")?;
        let mut candidates: Vec<_> = self
            .block_store
            .get_blocks_by_round(previous_round)
            .into_iter()
            .filter(|b| b.author() != self.authority)
            .filter_map(|b| {
                let rank = match self.support_of(target, &b) {
                    Support::Found(reference) if reference == desired => 0,
                    Support::Absent => 1,
                    Support::Found(_) => 2,
                    Support::Incomplete => return None,
                };
                Some((rank, b))
            })
            .collect();
        candidates.sort_by_key(|(rank, b)| (*rank, b.author(), *b.reference()));
        let mut parents = vec![*self_parent.reference()];
        let mut authors = BTreeSet::from([self.authority]);
        for (_, candidate) in candidates {
            if authors.insert(candidate.author()) {
                parents.push(*candidate.reference());
            }
            if self.has_quorum(&parents, previous_round) {
                break;
            }
        }
        if !self.has_quorum(&parents, previous_round) {
            return Err("local previous round lacks distinct-author quorum");
        }
        // Keep normal weak dependencies after the chosen strong parents. They cannot
        // precede the desired first support, and no honest block is fabricated.
        for reference in normal.includes().iter().skip(1) {
            if reference.authority != self.authority && !parents.contains(reference) {
                if !self.block_store.block_exists(*reference) {
                    return Err("normal dependency is not locally available");
                }
                parents.push(*reference);
            }
        }
        // Inspect exactly this parent order before signing, including the first self-parent.
        let unsigned = Data::new(StatementBlock::new(
            self.authority,
            normal.round(),
            parents.clone(),
            normal.statements().clone(),
            normal.meta_creation_time_ns(),
            normal.epoch_changed(),
            Default::default(),
        ));
        if self.find_support(target, &unsigned) != Support::Found(desired) {
            return Err("first DFS support is locked to another branch or has no desired support");
        }
        Ok(parents)
    }

    pub(crate) fn prepare_proposal(
        &self,
        normal: Data<StatementBlock>,
        signer: &Signer,
    ) -> (Data<StatementBlock>, Vec<Data<StatementBlock>>) {
        if matches!(self.config.mode, AdversaryMode::A0 | AdversaryMode::A1)
            || !self.is_byzantine(self.authority)
            || normal.author() != self.authority
            || normal.round() == 0
            || self.remaining_at(self.started.elapsed()).is_none()
        {
            return (normal, vec![]);
        }
        if normal.serialized_bytes().len() > MAX_ENTRY_SIZE / 2 {
            self.proposal_fallback(
                normal.round(),
                None,
                "normal proposal exceeds half of the WAL entry limit",
            );
            return (normal, vec![]);
        }
        // A retry/recovery must never create a third digest at an already persisted slot.
        let persisted = self.sorted_variants(self.authority, normal.round());
        if !persisted.is_empty() {
            self.proposal_fallback(
                normal.round(),
                None,
                "slot already persisted; reusing its primary",
            );
            let primary = self
                .block_store
                .get_own_blocks(normal.round() - 1, 1)
                .into_iter()
                .find(|b| b.round() == normal.round())
                .unwrap_or_else(|| persisted[0].clone());
            return (primary, vec![]);
        }
        let target = if matches!(
            self.config.mode,
            AdversaryMode::A3 | AdversaryMode::A4 | AdversaryMode::A5
        ) {
            self.target_for_round(normal.round())
        } else {
            None
        };
        if target.is_some() {
            let mut state = self.state.lock();
            if state.last_target != target {
                tracing::info!(event = "adversary_target", mode = ?self.config.mode, seed = self.config.seed,
                    authority = self.authority, round = normal.round(), target = ?target,
                    earliest_undecided = ?state.earliest_undecided, "Selected locally observed target or future Byzantine anchor");
                state.last_target = target;
            }
        }
        let directed = if let Some(target) = target {
            if normal.author_round() == (target.authority, target.round) {
                None // Create the two leader variants by parent order at the leader round.
            } else if normal.round() <= target.round {
                self.proposal_fallback(
                    normal.round(),
                    Some(target),
                    "target leader variants are not yet available",
                );
                None
            } else if self.config.mode == AdversaryMode::A3
                && normal.round() > target.round.saturating_add(self.wave_length - 2)
            {
                self.proposal_fallback(
                    normal.round(),
                    Some(target),
                    "A3 target is outside its actual vote window",
                );
                None
            } else {
                let leaders = self.sorted_variants(target.authority, target.round);
                if leaders.len() < 2 {
                    self.proposal_fallback(
                        normal.round(),
                        Some(target),
                        "fewer than two locally available leader variants",
                    );
                    None
                } else {
                    match (
                        self.directed_parents(&normal, target, *leaders[0].reference()),
                        self.directed_parents(&normal, target, *leaders[1].reference()),
                    ) {
                        (Ok(a), Ok(b)) if a != b => Some((a, b)),
                        (Err(reason), _) | (_, Err(reason)) => {
                            self.proposal_fallback(normal.round(), Some(target), reason);
                            None
                        }
                        _ => {
                            self.proposal_fallback(
                                normal.round(),
                                Some(target),
                                "directed parent orders are identical",
                            );
                            None
                        }
                    }
                }
            }
        } else {
            if matches!(
                self.config.mode,
                AdversaryMode::A3 | AdversaryMode::A4 | AdversaryMode::A5
            ) {
                self.proposal_fallback(
                    normal.round(),
                    None,
                    "no eligible Byzantine leader in the local vote window",
                );
            }
            None
        };
        let (primary, extra) = if let Some((a, b)) = directed {
            (
                self.sign_parents(&normal, a, signer),
                self.sign_parents(&normal, b, signer),
            )
        } else {
            let alternate = match self.nondirected_parents(&normal) {
                Ok(parents) => parents,
                Err(reason) => {
                    self.proposal_fallback(normal.round(), target, reason);
                    return (normal, vec![]);
                }
            };
            (
                normal.clone(),
                self.sign_parents(&normal, alternate, signer),
            )
        };
        let extra_bytes = extra.serialized_bytes().len().saturating_add(
            primary
                .serialized_bytes()
                .len()
                .saturating_sub(normal.serialized_bytes().len()),
        );
        let reason = if primary.reference() == extra.reference() {
            Some("fork digest is not distinct")
        } else if primary.serialized_bytes().len() > MAX_ENTRY_SIZE / 2
            || extra.serialized_bytes().len() > MAX_ENTRY_SIZE / 2
        {
            Some("fork exceeds half of the WAL entry limit")
        } else if extra_bytes > self.config.max_extra_bytes_per_round {
            Some("per-round extra byte budget exhausted")
        } else if primary.verify(&self.committee).is_err() || extra.verify(&self.committee).is_err()
        {
            Some("fork failed normal signature or threshold-clock verification")
        } else {
            None
        };
        if let Some(reason) = reason {
            self.proposal_fallback(normal.round(), target, reason);
            return (normal, vec![]);
        }
        for (role, block) in [&primary, &extra].into_iter().enumerate() {
            let variant =
                usize::from(block.reference() == primary.reference().max(extra.reference()));
            tracing::info!(event = "adversary_fork", mode = ?self.config.mode, seed = self.config.seed,
                authority = self.authority, reference = ?block.reference(), digest = ?block.digest(),
                target = ?target, variant, proposal_role = if role == 0 { "primary" } else { "extra" },
                bytes = block.serialized_bytes().len(), includes = ?block.includes(),
                "Prepared signed variant; caller persists primary before the extra");
        }
        (primary, vec![extra])
    }

    fn cohort(
        &self,
        peer: AuthorityIndex,
        target: Option<LeaderSlot>,
        round: RoundNumber,
    ) -> usize {
        if self.config.mode == AdversaryMode::A2 {
            return (mix(self.config.seed
                ^ peer.wrapping_mul(0x9e3779b97f4a7c15)
                ^ round.rotate_left(23))
                & 1) as usize;
        }
        let target_key = target.map_or(round, |t| t.round ^ t.authority.rotate_left(17));
        // Rotated adjacent cohorts remain balanced across honest identities.
        let honest: Vec<_> = self
            .committee
            .authorities()
            .filter(|a| !self.is_byzantine(*a))
            .collect();
        let rank = honest
            .iter()
            .position(|a| *a == peer)
            .unwrap_or(peer as usize);
        (rank + (mix(self.config.seed ^ target_key) & 1) as usize) % 2
    }

    fn preferred_variant(
        &self,
        peer: AuthorityIndex,
        variants: &[Data<StatementBlock>],
        target: Option<LeaderSlot>,
    ) -> usize {
        if variants.len() < 2 {
            return 0;
        }
        if matches!(
            self.config.mode,
            AdversaryMode::A3 | AdversaryMode::A4 | AdversaryMode::A5
        ) {
            if let Some(target) = target {
                let latest = self.block_store.last_seen_by_authority(peer);
                let observations = self.sorted_variants(peer, latest);
                let mut observed_support = None;
                let mut conflicting = false;
                for observed in observations {
                    if let Support::Found(reference) = self.support_of(target, &observed) {
                        if observed_support.map_or(false, |known| known != reference) {
                            conflicting = true;
                        }
                        observed_support = Some(reference);
                    }
                }
                if !conflicting {
                    if let Some(reference) = observed_support {
                        if let Some(index) = variants
                            .iter()
                            .position(|b| self.support_of(target, b) == Support::Found(reference))
                        {
                            return index;
                        }
                    }
                }
            }
        }
        self.cohort(peer, target, variants[0].round()) % variants.len()
    }

    fn dissemination_target(
        &self,
        block: &Data<StatementBlock>,
        variants: &[Data<StatementBlock>],
    ) -> Option<LeaderSlot> {
        let ancestry_target = self.target_for_round(block.round());
        if self.config.mode != AdversaryMode::A3 || self.config.target_round.is_some() {
            return ancestry_target;
        }
        // Keep DAG-directed vote routing when the variants have different actual
        // supports. Otherwise a fresh Byzantine leader's own fork is its target.
        if let Some(target) = ancestry_target {
            if variants.len() == 2 {
                if let (Support::Found(a), Support::Found(b)) = (
                    self.support_of(target, &variants[0]),
                    self.support_of(target, &variants[1]),
                ) {
                    if a != b {
                        return Some(target);
                    }
                }
            }
        }
        if self.is_byzantine(block.author())
            && self
                .config
                .target_authority
                .map_or(true, |a| a == block.author())
            && scheduled_leader(
                &self.committee,
                self.wave_length,
                self.number_of_leaders,
                self.pipeline,
                block.author(),
                block.round(),
            )
        {
            Some(LeaderSlot {
                round: block.round(),
                authority: block.author(),
            })
        } else {
            ancestry_target
        }
    }

    fn critical_certificate(&self, target: LeaderSlot, block: &Data<StatementBlock>) -> bool {
        let vote_round = target.round.saturating_add(self.wave_length - 2);
        if block.round() < vote_round.saturating_add(1) {
            return false;
        }
        for leader in self
            .sorted_variants(target.authority, target.round)
            .into_iter()
            .take(2)
        {
            let votes: Vec<_> = block
                .includes()
                .iter()
                .filter(|r| r.round == vote_round)
                .filter_map(|reference| self.block_store.get_block(*reference))
                .filter(|vote| {
                    self.find_support(target, vote) == Support::Found(*leader.reference())
                })
                .map(|vote| *vote.reference())
                .collect();
            if self.has_quorum(&votes, vote_round) {
                return true;
            }
        }
        false
    }

    fn delay_for(
        &self,
        peer: AuthorityIndex,
        block: &Data<StatementBlock>,
        variants: &[Data<StatementBlock>],
        target: Option<LeaderSlot>,
        elapsed: Duration,
    ) -> Duration {
        let Some(remaining) = self.remaining_at(elapsed) else {
            return Duration::ZERO;
        };
        let own_byzantine = self.is_byzantine(self.authority) && block.author() == self.authority;
        let delay = match self.config.mode {
            AdversaryMode::A0 => Duration::ZERO,
            AdversaryMode::A1 if own_byzantine => remaining,
            AdversaryMode::A1 => Duration::ZERO,
            _ => {
                let alternate = own_byzantine
                    && variants.len() == 2
                    && variants[self.preferred_variant(peer, variants, target)].reference()
                        != block.reference();
                let late_mode = matches!(self.config.mode, AdversaryMode::A4 | AdversaryMode::A5);
                let critical = late_mode
                    && target.map_or(false, |target| self.critical_certificate(target, block));
                if late_mode && (alternate || critical) {
                    Duration::from_millis(self.config.late_delay_ms)
                } else if alternate {
                    Duration::from_millis(self.config.delay_ms)
                } else if self.config.mode == AdversaryMode::A5
                    && !self.is_byzantine(self.authority)
                    && !self.is_byzantine(peer)
                {
                    // This applies to every block path, including forwards and exact fetches.
                    Duration::from_millis(self.config.delay_ms)
                } else {
                    Duration::ZERO
                }
            }
        };
        delay.min(remaining)
    }
}

fn mix(mut value: u64) -> u64 {
    value = (value ^ (value >> 30)).wrapping_mul(0xbf58476d1ce4e5b9);
    value = (value ^ (value >> 27)).wrapping_mul(0x94d049bb133111eb);
    value ^ (value >> 31)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum SendPath {
    Own,
    Forward,
    Fetch,
}

#[derive(Clone)]
pub(crate) struct ScheduledSender {
    raw: mpsc::Sender<NetworkMessage>,
    connection: Option<Arc<ConnectionQueue>>,
}

struct ConnectionQueue {
    adversary: Arc<Adversary>,
    peer: AuthorityIndex,
    queue: Mutex<QueueState>,
    changed: Notify,
    closed: AtomicBool,
}

#[derive(Default)]
struct QueueState {
    pending: BTreeMap<BlockReference, PendingMessage>,
    in_flight: Option<BlockReference>,
}

struct PendingMessage {
    block: Data<StatementBlock>,
    deadline: Duration,
    path: SendPath,
    target: Option<LeaderSlot>,
    variant: usize,
    peer: AuthorityIndex,
    delivered: bool,
    budget: PendingBudget,
}

struct PendingBudget {
    owner: Weak<Adversary>,
    bytes: usize,
}

impl Drop for PendingBudget {
    fn drop(&mut self) {
        if let Some(owner) = self.owner.upgrade() {
            let mut state = owner.state.lock();
            state.pending_messages -= 1;
            state.pending_bytes -= self.bytes;
        }
    }
}

impl Drop for PendingMessage {
    fn drop(&mut self) {
        if !self.delivered {
            if let Some(owner) = self.budget.owner.upgrade() {
                owner.log_message(
                    "adversary_abandon",
                    self.peer,
                    self.path,
                    &self.block,
                    self.target,
                    self.variant,
                    "connection or scheduler closed before delivery",
                );
            }
        }
    }
}

struct SchedulerExit {
    connection: Arc<ConnectionQueue>,
}

impl Drop for SchedulerExit {
    fn drop(&mut self) {
        self.connection.closed.store(true, Ordering::Release);
        let abandoned = {
            let mut queue = self.connection.queue.lock();
            queue.in_flight = None;
            std::mem::take(&mut queue.pending)
        };
        // Drop payloads and RAII reservations after releasing the connection mutex.
        drop(abandoned);
        self.connection.changed.notify_waiters();
    }
}

impl Adversary {
    pub(crate) fn sender(
        self: &Arc<Self>,
        peer: AuthorityIndex,
        raw: mpsc::Sender<NetworkMessage>,
    ) -> (ScheduledSender, Option<runtime::JoinHandle<()>>) {
        assert!(
            self.committee.known_authority(peer),
            "Adversary peer is not in committee"
        );
        if self.config.mode == AdversaryMode::A0
            || (self.config.mode != AdversaryMode::A5 && !self.is_byzantine(self.authority))
        {
            return (
                ScheduledSender {
                    raw,
                    connection: None,
                },
                None,
            );
        }
        let connection = Arc::new(ConnectionQueue {
            adversary: self.clone(),
            peer,
            queue: Mutex::new(QueueState::default()),
            changed: Notify::new(),
            closed: AtomicBool::new(false),
        });
        let task_connection = connection.clone();
        let task_raw = raw.clone();
        // Construct before spawn: even aborting an unpolled future drops all reservations.
        let cleanup = SchedulerExit {
            connection: connection.clone(),
        };
        let handle = runtime::Handle::current().spawn(async move {
            let _cleanup = cleanup;
            run_scheduler(task_connection, task_raw).await;
        });
        (
            ScheduledSender {
                raw,
                connection: Some(connection),
            },
            Some(handle),
        )
    }

    fn reserve(self: &Arc<Self>, bytes: usize) -> Option<PendingBudget> {
        let mut state = self.state.lock();
        let messages = state.pending_messages.checked_add(1)?;
        let total_bytes = state.pending_bytes.checked_add(bytes)?;
        if messages > self.config.max_pending_messages
            || total_bytes > self.config.max_pending_bytes
        {
            return None;
        }
        state.pending_messages = messages;
        state.pending_bytes = total_bytes;
        Some(PendingBudget {
            owner: Arc::downgrade(self),
            bytes,
        })
    }

    fn log_message(
        &self,
        event: &'static str,
        peer: AuthorityIndex,
        path: SendPath,
        block: &Data<StatementBlock>,
        target: Option<LeaderSlot>,
        variant: usize,
        reason: &str,
    ) {
        tracing::info!(event, mode = ?self.config.mode, seed = self.config.seed, authority = self.authority,
            peer, path = ?path, reference = ?block.reference(), digest = ?block.digest(),
            target = ?target, variant, bytes = block.serialized_bytes().len(), reason,
            "Adversary block scheduling event");
    }
}

impl ScheduledSender {
    pub(crate) async fn send_block(
        &self,
        block: Data<StatementBlock>,
        path: SendPath,
    ) -> Option<()> {
        let Some(connection) = &self.connection else {
            return self.raw.send(NetworkMessage::Block(block)).await.ok();
        };
        if self.raw.is_closed() || connection.closed.load(Ordering::Acquire) {
            return None;
        }
        let adversary = &connection.adversary;
        let own =
            block.author() == adversary.authority && adversary.is_byzantine(adversary.authority);
        let mut variants = if own {
            adversary.sorted_variants(block.author(), block.round())
        } else {
            vec![block.clone()]
        };
        if variants.is_empty() {
            variants.push(block.clone());
        }
        if variants.len() > 2 {
            adversary.log_message(
                "adversary_fallback",
                connection.peer,
                path,
                &block,
                None,
                0,
                "more than two persisted variants; sending only the supplied block",
            );
            variants = vec![block.clone()];
        }
        // Own gossip enumerates BOTH persisted variants. Fetch/Forward always use the
        // supplied exact digest, including when another variant is preferred for this peer.
        let outgoing = if path == SendPath::Own && own {
            variants.clone()
        } else {
            vec![block]
        };
        for block in outgoing {
            let variant = variants
                .iter()
                .position(|b| b.reference() == block.reference())
                .unwrap_or(0);
            self.send_one(connection, block, path, &variants, variant)
                .await?;
        }
        Some(())
    }

    async fn send_one(
        &self,
        connection: &Arc<ConnectionQueue>,
        block: Data<StatementBlock>,
        path: SendPath,
        variants: &[Data<StatementBlock>],
        variant: usize,
    ) -> Option<()> {
        let adversary = &connection.adversary;
        let elapsed = adversary.started.elapsed();
        let target = if matches!(
            adversary.config.mode,
            AdversaryMode::A3 | AdversaryMode::A4 | AdversaryMode::A5
        ) {
            adversary.dissemination_target(&block, variants)
        } else {
            None
        };
        let delay = adversary.delay_for(connection.peer, &block, variants, target, elapsed);
        #[derive(Clone, Copy)]
        enum Action {
            Queued,
            Duplicate,
            Immediate,
            Overflow,
            Closed,
        }
        let action = {
            let mut queue = connection.queue.lock();
            if connection.closed.load(Ordering::Acquire) {
                Action::Closed
            } else if queue.pending.contains_key(block.reference())
                || queue.in_flight == Some(*block.reference())
            {
                Action::Duplicate // Keep the first deadline, including across gossip and fetch.
            } else if delay.is_zero() {
                Action::Immediate
            } else if let Some(budget) = adversary.reserve(block.serialized_bytes().len()) {
                queue.pending.insert(
                    *block.reference(),
                    PendingMessage {
                        block: block.clone(),
                        deadline: elapsed + delay,
                        path,
                        target,
                        variant,
                        peer: connection.peer,
                        delivered: false,
                        budget,
                    },
                );
                Action::Queued
            } else {
                Action::Overflow
            }
        };
        match action {
            Action::Closed => None,
            Action::Duplicate => Some(()),
            Action::Queued => {
                tracing::info!(event = "adversary_schedule", mode = ?adversary.config.mode,
                    seed = adversary.config.seed, authority = adversary.authority, peer = connection.peer,
                    path = ?path, reference = ?block.reference(), digest = ?block.digest(), target = ?target,
                    variant, bytes = block.serialized_bytes().len(), delay_ms = delay.as_millis() as u64,
                    deadline_ms = (elapsed + delay).as_millis() as u64, "Queued finite block delay");
                connection.changed.notify_one();
                Some(())
            }
            Action::Immediate | Action::Overflow => {
                if matches!(action, Action::Overflow) {
                    adversary.log_message(
                        "adversary_overflow",
                        connection.peer,
                        path,
                        &block,
                        target,
                        variant,
                        "global pending budget exhausted; releasing this block immediately",
                    );
                }
                self.raw
                    .send(NetworkMessage::Block(block.clone()))
                    .await
                    .ok()?;
                adversary.log_message(
                    "adversary_deliver",
                    connection.peer,
                    path,
                    &block,
                    target,
                    variant,
                    "immediate",
                );
                Some(())
            }
        }
    }
}

async fn run_scheduler(connection: Arc<ConnectionQueue>, raw: mpsc::Sender<NetworkMessage>) {
    enum Next {
        Deliver(PendingMessage),
        Wait(Duration),
        Empty,
    }
    loop {
        let next = {
            let mut queue = connection.queue.lock();
            let earliest = queue
                .pending
                .iter()
                .min_by_key(|(_, message)| message.deadline)
                .map(|(reference, message)| (*reference, message.deadline));
            match earliest {
                Some((reference, deadline)) => {
                    let now = connection.adversary.started.elapsed();
                    if deadline <= now {
                        queue.in_flight = Some(reference);
                        Next::Deliver(
                            queue
                                .pending
                                .remove(&reference)
                                .expect("selected pending block exists"),
                        )
                    } else {
                        Next::Wait(deadline - now)
                    }
                }
                None => Next::Empty,
            }
        };
        match next {
            Next::Deliver(mut message) => {
                if raw
                    .send(NetworkMessage::Block(message.block.clone()))
                    .await
                    .is_err()
                {
                    return;
                }
                message.delivered = true;
                connection.adversary.log_message(
                    "adversary_deliver",
                    connection.peer,
                    message.path,
                    &message.block,
                    message.target,
                    message.variant,
                    "scheduled deadline reached",
                );
                connection.queue.lock().in_flight = None;
                // message drops its budget before the next dequeue.
            }
            Next::Wait(delay) => {
                tokio::select! {
                    // Bound each timer construction even for unusually long configured windows.
                    _ = runtime::sleep(delay.min(Duration::from_secs(60))) => {},
                    _ = connection.changed.notified() => {},
                    _ = raw.closed() => return,
                }
            }
            Next::Empty => {
                tokio::select! {
                    _ = connection.changed.notified() => {},
                    _ = raw.closed() => return,
                }
            }
        }
    }
}

#[cfg(all(test, not(feature = "simulator")))]
mod tests {
    use super::*;
    use crate::{
        block_store::BlockWriter,
        crypto::{dummy_signer, with_real_crypto_for_test},
        test_util::TestBlockWriter,
        types::{BaseStatement, Transaction},
    };

    fn config(mode: AdversaryMode) -> AdversaryConfig {
        AdversaryConfig {
            mode,
            byzantine_authorities: if mode == AdversaryMode::A0 {
                vec![]
            } else {
                vec![0]
            },
            ..AdversaryConfig::default()
        }
    }

    fn fixture() -> (Arc<Committee>, TestBlockWriter, Vec<BlockReference>) {
        let committee = Committee::new_for_benchmarks(4);
        let mut writer = TestBlockWriter::new(&committee);
        let genesis: Vec<_> = committee
            .authorities()
            .map(StatementBlock::new_genesis)
            .collect();
        let references = genesis.iter().map(|b| *b.reference()).collect();
        for block in genesis {
            writer.insert_block(block);
        }
        (committee, writer, references)
    }

    fn block(
        authority: AuthorityIndex,
        round: RoundNumber,
        mut includes: Vec<BlockReference>,
    ) -> Data<StatementBlock> {
        if let Some(index) = includes.iter().position(|r| r.authority == authority) {
            includes.swap(0, index);
        }
        Data::new(StatementBlock::new_with_signer(
            authority,
            round,
            includes,
            vec![BaseStatement::Share(Transaction::new(vec![7, 11, 13]))],
            987654,
            false,
            &dummy_signer(),
        ))
    }

    fn adversary(
        config: AdversaryConfig,
        authority: AuthorityIndex,
        committee: Arc<Committee>,
        writer: &TestBlockWriter,
    ) -> Arc<Adversary> {
        Adversary::new(
            config,
            authority,
            committee,
            writer.block_store(),
            3,
            1,
            true,
        )
    }

    #[test]
    fn rejects_invalid_identity_stake_timing_and_leader_targets() {
        let committee = Committee::new_for_benchmarks(4);
        let mut cfg = config(AdversaryMode::A3);
        assert!(cfg.validate(&committee, 3, 1, true).is_ok());
        cfg.byzantine_authorities = vec![0, 0];
        assert!(cfg.validate(&committee, 3, 1, true).is_err());
        cfg.byzantine_authorities = vec![0, 1];
        assert!(cfg.validate(&committee, 3, 1, true).is_err());
        cfg.byzantine_authorities = vec![4];
        assert!(cfg.validate(&committee, 3, 1, true).is_err());
        cfg = config(AdversaryMode::A3);
        cfg.duration_ms = 0;
        assert!(cfg.validate(&committee, 3, 1, true).is_err());
        cfg = config(AdversaryMode::A3);
        cfg.start_after_ms = u64::MAX;
        assert!(cfg.validate(&committee, 3, 1, true).is_err());
        cfg = config(AdversaryMode::A3);
        cfg.target_authority = Some(1);
        assert!(cfg.validate(&committee, 3, 1, true).is_err());
        cfg.target_authority = Some(0);
        cfg.target_round = Some(4);
        assert!(cfg.validate(&committee, 3, 1, true).is_ok());
        assert!(cfg.validate(&committee, 3, 1, false).is_err());
        cfg.target_round = Some(3);
        assert!(cfg.validate(&committee, 3, 1, true).is_err());
        assert!(cfg.validate(&committee, 3, 2, true).is_ok());
        cfg.target_round = Some(0);
        assert!(cfg.validate(&committee, 3, 2, true).is_err());
        cfg = config(AdversaryMode::A0);
        cfg.byzantine_authorities = vec![0];
        assert!(cfg.validate(&committee, 3, 1, true).is_err());
        assert!(serde_yaml::from_str::<AdversaryConfig>("mode: A2\nunknown_option: 1\n").is_err());
    }

    #[test]
    fn a0_and_honest_authors_preserve_serialized_proposal() {
        with_real_crypto_for_test(|| {
            let (committee, writer, genesis) = fixture();
            for (mode, authority) in [
                (AdversaryMode::A0, 0),
                (AdversaryMode::A5, 1),
                (AdversaryMode::A1, 0),
            ] {
                let normal = block(authority, 1, genesis.clone());
                let original = normal.serialized_bytes().to_vec();
                let adversary = adversary(config(mode), authority, committee.clone(), &writer);
                let (primary, extras) = adversary.prepare_proposal(normal, &dummy_signer());
                assert_eq!(primary.serialized_bytes().as_ref(), original.as_slice());
                assert!(extras.is_empty());
            }
        });
    }

    #[test]
    fn a2_produces_two_valid_distinct_forks_with_identical_payload() {
        with_real_crypto_for_test(|| {
            let (committee, writer, genesis) = fixture();
            let normal = block(0, 1, genesis);
            let adversary = adversary(config(AdversaryMode::A2), 0, committee.clone(), &writer);
            let (primary, extras) = adversary.prepare_proposal(normal.clone(), &dummy_signer());
            assert_eq!(extras.len(), 1);
            let alternate = &extras[0];
            primary.verify(&committee).unwrap();
            alternate.verify(&committee).unwrap();
            assert_ne!(primary.reference(), alternate.reference());
            assert_eq!(primary.author_round(), alternate.author_round());
            assert_eq!(primary.statements(), alternate.statements());
            assert_eq!(
                primary.meta_creation_time_ns(),
                alternate.meta_creation_time_ns()
            );
            assert_eq!(primary.epoch_changed(), alternate.epoch_changed());
            assert_eq!(primary.includes()[0], alternate.includes()[0]);
            assert_eq!(primary.serialized_bytes(), normal.serialized_bytes());
        });
    }

    #[test]
    fn equivocations_do_not_supply_two_identities_to_quorum() {
        with_real_crypto_for_test(|| {
            let (committee, writer, genesis) = fixture();
            let a = block(0, 1, genesis.clone());
            let mut swapped = genesis;
            swapped.swap(1, 2);
            let b = block(0, 1, swapped.clone());
            let c = block(1, 1, swapped);
            let adversary = adversary(config(AdversaryMode::A2), 0, committee, &writer);
            assert!(!adversary.has_quorum(&[*a.reference(), *b.reference(), *c.reference()], 1));
        });
    }

    #[test]
    fn directed_forks_and_recipient_choice_follow_first_dag_support() {
        with_real_crypto_for_test(|| {
            let (committee, mut writer, mut previous) = fixture();
            for round in 1..4 {
                previous = committee
                    .authorities()
                    .map(|a| {
                        let b = block(a, round, previous.clone());
                        writer.insert_block(b.clone());
                        *b.reference()
                    })
                    .collect();
            }
            let mut cfg = config(AdversaryMode::A3);
            cfg.target_authority = Some(0);
            cfg.target_round = Some(4);
            let adversary = adversary(cfg, 0, committee.clone(), &writer);
            let (leader, extra) =
                adversary.prepare_proposal(block(0, 4, previous.clone()), &dummy_signer());
            assert_eq!(extra.len(), 1);
            let alternate_leader = extra[0].clone();
            writer.insert_block(leader.clone());
            writer.insert_block(alternate_leader.clone());
            let mut round_four = vec![*leader.reference()];
            for a in 1..4 {
                let b = block(a, 4, previous.clone());
                round_four.push(*b.reference());
                writer.insert_block(b);
            }
            let (primary_vote, extra_vote) =
                adversary.prepare_proposal(block(0, 5, round_four.clone()), &dummy_signer());
            assert_eq!(extra_vote.len(), 1);
            let target = LeaderSlot {
                authority: 0,
                round: 4,
            };
            let support_a = adversary.find_support(target, &primary_vote);
            let support_b = adversary.find_support(target, &extra_vote[0]);
            assert_ne!(support_a, support_b);
            primary_vote.verify(&committee).unwrap();
            extra_vote[0].verify(&committee).unwrap();
            writer.insert_block(primary_vote.clone());
            writer.insert_block(extra_vote[0].clone());
            round_four[0] = *alternate_leader.reference();
            let recipient = block(1, 5, round_four);
            writer.insert_block(recipient);
            let variants = vec![primary_vote, extra_vote[0].clone()];
            let preferred = adversary.preferred_variant(1, &variants, Some(target));
            assert_eq!(
                adversary.find_support(target, &variants[preferred]),
                Support::Found(*alternate_leader.reference())
            );
        });
    }

    #[test]
    fn observer_rolls_forward_and_fixed_target_does_not_drift() {
        let (committee, writer, _) = fixture();
        let adversary = adversary(config(AdversaryMode::A4), 0, committee.clone(), &writer);
        let statuses = [LeaderStatus::Undecided(3, 3), LeaderStatus::Undecided(0, 4)];
        adversary.observe_leaders(statuses.iter());
        assert_eq!(
            adversary.target_for_round(4),
            Some(LeaderSlot {
                authority: 0,
                round: 8
            })
        );
        let statuses = [
            LeaderStatus::Skip(3, 3),
            LeaderStatus::Skip(0, 4),
            LeaderStatus::Undecided(1, 5),
        ];
        adversary.observe_leaders(statuses.iter());
        assert_eq!(
            adversary.target_for_round(6),
            Some(LeaderSlot {
                authority: 0,
                round: 8
            })
        );
        let mut cfg = config(AdversaryMode::A4);
        cfg.target_authority = Some(0);
        cfg.target_round = Some(4);
        let fixed = Adversary::new(cfg, 0, committee, writer.block_store(), 3, 1, true);
        fixed.observe_leaders(statuses.iter());
        assert_eq!(
            fixed.target_for_round(8),
            Some(LeaderSlot {
                authority: 0,
                round: 4
            })
        );
    }

    #[test]
    fn consecutive_byzantine_leaders_do_not_replace_an_active_vote_target() {
        with_real_crypto_for_test(|| {
            let committee = Committee::new_for_benchmarks(7);
            let mut writer = TestBlockWriter::new(&committee);
            let mut previous: Vec<_> = committee
                .authorities()
                .map(|a| {
                    let genesis = StatementBlock::new_genesis(a);
                    writer.insert_block(genesis.clone());
                    *genesis.reference()
                })
                .collect();
            for round in 1..7 {
                previous = committee
                    .authorities()
                    .map(|a| {
                        let b = block(a, round, previous.clone());
                        writer.insert_block(b.clone());
                        *b.reference()
                    })
                    .collect();
            }
            let mut cfg = config(AdversaryMode::A3);
            cfg.byzantine_authorities = vec![0, 1];
            let adversary = adversary(cfg, 0, committee.clone(), &writer);
            let (leader, extras) =
                adversary.prepare_proposal(block(0, 7, previous.clone()), &dummy_signer());
            assert_eq!(extras.len(), 1);
            writer.insert_block(leader.clone());
            writer.insert_block(extras[0].clone());
            let mut parent_layer = vec![*leader.reference()];
            for a in 1..7 {
                let b = block(a, 7, previous.clone());
                parent_layer.push(*b.reference());
                writer.insert_block(b);
            }
            let target = LeaderSlot {
                round: 7,
                authority: 0,
            };
            assert_eq!(adversary.target_for_round(8), Some(target));
            let (vote, alternate) =
                adversary.prepare_proposal(block(0, 8, parent_layer), &dummy_signer());
            assert_eq!(alternate.len(), 1);
            assert_ne!(
                adversary.find_support(target, &vote),
                adversary.find_support(target, &alternate[0])
            );
            vote.verify(&committee).unwrap();
            alternate[0].verify(&committee).unwrap();
        });
    }

    #[test]
    fn oversized_extra_budget_falls_back_to_original_proposal() {
        with_real_crypto_for_test(|| {
            let (committee, writer, genesis) = fixture();
            let mut cfg = config(AdversaryMode::A2);
            cfg.max_extra_bytes_per_round = 1;
            let adversary = adversary(cfg, 0, committee, &writer);
            let normal = block(0, 1, genesis);
            let original = normal.serialized_bytes().to_vec();
            let (primary, extras) = adversary.prepare_proposal(normal, &dummy_signer());
            assert_eq!(primary.serialized_bytes().as_ref(), original.as_slice());
            assert!(extras.is_empty());
        });
    }

    #[tokio::test]
    async fn own_gossip_sends_both_variants_with_delayed_alternate_and_fixed_deadline() {
        for mode in [AdversaryMode::A2, AdversaryMode::A4] {
            let (committee, mut writer, genesis) = fixture();
            let mut cfg = config(mode);
            cfg.duration_ms = 160;
            cfg.delay_ms = 120;
            cfg.late_delay_ms = 1_000;
            let adversary = adversary(cfg, 0, committee, &writer);
            let (primary, extras) = with_real_crypto_for_test(|| {
                adversary.prepare_proposal(block(0, 1, genesis), &dummy_signer())
            });
            assert_eq!(extras.len(), 1);
            writer.insert_block(primary.clone());
            writer.insert_block(extras[0].clone());
            let (raw, mut receiver) = mpsc::channel(8);
            let (sender, handle) = adversary.sender(1, raw);
            sender.send_block(primary, SendPath::Own).await.unwrap();
            let NetworkMessage::Block(immediate) = receiver.try_recv().unwrap() else {
                panic!("expected immediate preferred variant");
            };
            let connection = sender.connection.as_ref().unwrap();
            let (pending, first_deadline) = {
                let queue = connection.queue.lock();
                assert_eq!(queue.pending.len(), 1);
                let message = queue.pending.values().next().unwrap();
                (message.block.clone(), message.deadline)
            };
            assert_ne!(pending.reference(), immediate.reference());
            tokio::time::sleep(Duration::from_millis(5)).await;
            sender
                .send_block(pending.clone(), SendPath::Fetch)
                .await
                .unwrap();
            assert_eq!(
                connection
                    .queue
                    .lock()
                    .pending
                    .values()
                    .next()
                    .unwrap()
                    .deadline,
                first_deadline
            );
            let released = tokio::time::timeout(Duration::from_secs(1), receiver.recv())
                .await
                .unwrap()
                .unwrap();
            assert!(
                matches!(released, NetworkMessage::Block(b) if b.reference() == pending.reference())
            );
            assert!(receiver.try_recv().is_err());
            let handle = handle.unwrap();
            handle.abort();
            let _ = handle.await;
        }
    }

    #[tokio::test]
    async fn a5_delays_honest_own_forward_and_exact_fetch_paths() {
        let (committee, writer, genesis) = fixture();
        let mut cfg = config(AdversaryMode::A5);
        cfg.delay_ms = 50;
        let adversary = adversary(cfg, 1, committee, &writer);
        let (raw, mut receiver) = mpsc::channel(8);
        let (sender, handle) = adversary.sender(2, raw);
        let mut requested = BTreeSet::new();
        for (author, path) in [
            (1, SendPath::Own),
            (2, SendPath::Forward),
            (3, SendPath::Fetch),
        ] {
            let b = block(author, 1, genesis.clone());
            requested.insert(*b.reference());
            sender.send_block(b, path).await.unwrap();
        }
        assert!(receiver.try_recv().is_err());
        assert_eq!(adversary.state.lock().pending_messages, 3);
        let mut delivered = BTreeSet::new();
        for _ in 0..3 {
            let message = tokio::time::timeout(Duration::from_secs(1), receiver.recv())
                .await
                .unwrap()
                .unwrap();
            let NetworkMessage::Block(b) = message else {
                panic!("expected block");
            };
            delivered.insert(*b.reference());
        }
        assert_eq!(delivered, requested);
        let handle = handle.unwrap();
        handle.abort();
        let _ = handle.await;
    }

    #[tokio::test]
    async fn byte_budget_overflow_releases_immediately_without_reservation() {
        let (committee, writer, genesis) = fixture();
        let b = block(0, 1, genesis);
        let mut cfg = config(AdversaryMode::A1);
        cfg.max_pending_bytes = b.serialized_bytes().len() - 1;
        let adversary = adversary(cfg, 0, committee, &writer);
        let (raw, mut receiver) = mpsc::channel(8);
        let (sender, handle) = adversary.sender(1, raw);
        sender.send_block(b, SendPath::Fetch).await.unwrap();
        assert!(matches!(
            receiver.try_recv().unwrap(),
            NetworkMessage::Block(_)
        ));
        assert_eq!(adversary.state.lock().pending_messages, 0);
        assert_eq!(adversary.state.lock().pending_bytes, 0);
        let handle = handle.unwrap();
        handle.abort();
        let _ = handle.await;
    }

    #[cfg(not(feature = "simulator"))]
    #[tokio::test]
    async fn exact_fetch_is_delayed_without_digest_substitution_and_releases() {
        let (committee, mut writer, genesis) = fixture();
        let (normal, alternate) = with_real_crypto_for_test(|| {
            let normal = block(0, 1, genesis.clone());
            let mut parents = genesis;
            parents.swap(1, 2);
            (normal, block(0, 1, parents))
        });
        writer.insert_block(normal.clone());
        writer.insert_block(alternate.clone());
        let mut cfg = config(AdversaryMode::A1);
        cfg.duration_ms = 70;
        let adversary = adversary(cfg, 0, committee, &writer);
        let (raw, mut receiver) = mpsc::channel(8);
        let (sender, handle) = adversary.sender(1, raw);
        sender
            .send_block(alternate.clone(), SendPath::Fetch)
            .await
            .unwrap();
        assert!(
            tokio::time::timeout(Duration::from_millis(10), receiver.recv())
                .await
                .is_err()
        );
        let delivered = tokio::time::timeout(Duration::from_secs(1), receiver.recv())
            .await
            .unwrap()
            .unwrap();
        assert!(
            matches!(delivered, NetworkMessage::Block(b) if b.reference() == alternate.reference())
        );
        sender
            .send_block(normal.clone(), SendPath::Fetch)
            .await
            .unwrap();
        let delivered = tokio::time::timeout(Duration::from_millis(30), receiver.recv())
            .await
            .unwrap()
            .unwrap();
        assert!(
            matches!(delivered, NetworkMessage::Block(b) if b.reference() == normal.reference())
        );
        let handle = handle.unwrap();
        handle.abort();
        let _ = handle.await;
    }

    #[cfg(not(feature = "simulator"))]
    #[tokio::test]
    async fn global_budget_fails_open_and_abort_releases_all_reservations() {
        let (committee, writer, genesis) = fixture();
        let normal = block(0, 1, genesis);
        let mut cfg = config(AdversaryMode::A1);
        cfg.max_pending_messages = 1;
        let adversary = adversary(cfg, 0, committee, &writer);
        let (raw_a, _receiver_a) = mpsc::channel(8);
        let (raw_b, mut receiver_b) = mpsc::channel(8);
        let (sender_a, task_a) = adversary.sender(1, raw_a);
        let (sender_b, task_b) = adversary.sender(2, raw_b);
        sender_a
            .send_block(normal.clone(), SendPath::Fetch)
            .await
            .unwrap();
        sender_b.send_block(normal, SendPath::Fetch).await.unwrap();
        assert!(matches!(
            receiver_b.try_recv().unwrap(),
            NetworkMessage::Block(_)
        ));
        assert_eq!(adversary.state.lock().pending_messages, 1);
        for task in [task_a, task_b].into_iter().flatten() {
            task.abort();
            let _ = task.await;
        }
        assert_eq!(adversary.state.lock().pending_messages, 0);
        assert_eq!(adversary.state.lock().pending_bytes, 0);
    }
}
