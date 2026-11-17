// Copyright (c) Mysten Labs, Inc.
// SPDX-License-Identifier: Apache-2.0

use crate::metrics::{Metrics, UtilizationTimerExt};
use crate::state::{RecoveredState, RecoveredStateBuilder};
use crate::types::{AuthorityIndex, BlockDigest, BlockReference, RoundNumber, StatementBlock};
use crate::wal::{Tag, WalPosition, WalReader, WalWriter};
use crate::{committee::Committee, types::TransactionLocator};
use crate::{consensus::linearizer::CommittedSubDag, types::Transaction};
use crate::{data::Data, types::BaseStatement};
use minibytes::Bytes;
use parking_lot::RwLock;
use serde::{Deserialize, Serialize};
use std::cmp::max;
use std::collections::{BTreeMap, HashMap};
use std::io::IoSlice;
use std::sync::Arc;
use std::time::Instant;

#[derive(Clone)]
pub struct BlockStore {
    inner: Arc<RwLock<BlockStoreInner>>,
    block_wal_reader: Arc<WalReader>,
    metrics: Arc<Metrics>,
}

#[derive(Default)]
struct BlockStoreInner {
    index: BTreeMap<RoundNumber, HashMap<(AuthorityIndex, BlockDigest), IndexEntry>>,
    own_blocks: BTreeMap<RoundNumber, BlockDigest>,
    highest_round: RoundNumber,
    authority: AuthorityIndex,
    last_seen_by_authority: Vec<RoundNumber>,
    last_own_block: Option<BlockReference>,
}

pub trait BlockWriter {
    fn insert_block(&mut self, block: Data<StatementBlock>) -> WalPosition;
    fn insert_own_block(&mut self, block: &OwnBlockData);
}

#[derive(Clone)]
enum IndexEntry {
    WalPosition(WalPosition),
    Loaded(WalPosition, Data<StatementBlock>),
}

impl BlockStore {
    pub fn open(
        authority: AuthorityIndex,
        block_wal_reader: Arc<WalReader>,
        wal_writer: &WalWriter,
        metrics: Arc<Metrics>,
        committee: &Committee,
    ) -> RecoveredState {
        let last_seen_by_authority = committee.authorities().map(|_| 0).collect();
        let mut inner = BlockStoreInner {
            authority,
            last_seen_by_authority,
            ..Default::default()
        };
        let mut builder = RecoveredStateBuilder::new();
        let mut replay_started: Option<Instant> = None;
        let mut block_count = 0u64;
        for (pos, (tag, data)) in block_wal_reader.iter_until(wal_writer) {
            if replay_started.is_none() {
                replay_started = Some(Instant::now());
                tracing::info!("Wal is not empty, starting replay");
            }
            let block = match tag {
                WAL_ENTRY_BLOCK => {
                    let block = Data::<StatementBlock>::from_bytes(data)
                        .expect("Failed to deserialize data from wal");
                    builder.block(pos, &block);
                    block
                }
                WAL_ENTRY_ADVERSARY_BLOCK => {
                    // Extra local forks are available by digest, but are not replayed
                    // as another application proposal or as pending received payload.
                    Data::<StatementBlock>::from_bytes(data)
                        .expect("Failed to deserialize adversary fork from wal")
                }
                WAL_ENTRY_PAYLOAD => {
                    builder.payload(pos, data);
                    continue;
                }
                WAL_ENTRY_OWN_BLOCK => {
                    let (own_block_data, own_block) = OwnBlockData::from_bytes(data)
                        .expect("Failed to deserialized own block data from wal");
                    builder.own_block(own_block_data);
                    own_block
                }
                WAL_ENTRY_STATE => {
                    builder.state(data);
                    continue;
                }
                WAL_ENTRY_COMMIT => {
                    let (commit_data, state) = bincode::deserialize(&data)
                        .expect("Failed to deserialized commit data from wal");
                    builder.commit_data(commit_data, state);
                    continue;
                }
                _ => panic!("Unknown wal tag {tag} at position {pos}"),
            };
            // todo - we want to keep some last blocks in the cache
            block_count += 1;
            inner.add_unloaded(block.reference(), pos);
        }
        metrics.block_store_entries.inc_by(block_count);
        if let Some(replay_started) = replay_started {
            tracing::info!("Wal replay completed in {:?}", replay_started.elapsed());
        } else {
            tracing::info!("Wal is empty, will start from genesis");
        }
        let this = Self {
            block_wal_reader,
            inner: Arc::new(RwLock::new(inner)),
            metrics,
        };
        builder.build(this)
    }

    pub fn insert_block(&self, block: Data<StatementBlock>, position: WalPosition) {
        self.metrics.block_store_entries.inc();
        self.inner.write().add_loaded(position, block);
    }

    pub(crate) fn persist_own_block_with_forks(
        &self,
        primary: &OwnBlockData,
        forks: Vec<Data<StatementBlock>>,
        writer: &mut WalWriter,
    ) {
        assert!(forks.len() <= 1, "At most two versions per own proposal");
        let mut entries = vec![(primary.write_to_wal(writer), primary.block.clone())];
        for fork in forks {
            assert_eq!(fork.author_round(), primary.block.author_round());
            let position = writer
                .write(WAL_ENTRY_ADVERSARY_BLOCK, fork.serialized_bytes())
                .expect("Writing adversary fork to wal failed");
            entries.push((position, fork));
        }
        self.metrics
            .block_store_entries
            .inc_by(entries.len() as u64);
        // Publish the primary cursor and every alternative in one critical section.
        // A streaming reader must never advance past a half-published fork pair.
        let mut inner = self.inner.write();
        for (position, block) in entries {
            inner.add_loaded(position, block);
        }
    }

    pub fn get_block(&self, reference: BlockReference) -> Option<Data<StatementBlock>> {
        let entry = self.inner.read().get_block(reference);
        // todo - consider adding loaded entries back to cache
        entry.map(|pos| self.read_index(pos))
    }

    pub fn get_blocks_by_round(&self, round: RoundNumber) -> Vec<Data<StatementBlock>> {
        let entries = self.inner.read().get_blocks_by_round(round);
        self.read_index_vec(entries)
    }

    pub fn get_blocks_at_authority_round(
        &self,
        authority: AuthorityIndex,
        round: RoundNumber,
    ) -> Vec<Data<StatementBlock>> {
        let entries = self
            .inner
            .read()
            .get_blocks_at_authority_round(authority, round);
        self.read_index_vec(entries)
    }

    pub fn block_exists_at_authority_round(
        &self,
        authority: AuthorityIndex,
        round: RoundNumber,
    ) -> bool {
        let inner = self.inner.read();
        let Some(blocks) = inner.index.get(&round) else {
            return false;
        };
        blocks
            .keys()
            .any(|(block_authority, _)| *block_authority == authority)
    }

    pub fn all_blocks_exists_at_authority_round(
        &self,
        authorities: &[AuthorityIndex],
        round: RoundNumber,
    ) -> bool {
        let inner = self.inner.read();
        let Some(blocks) = inner.index.get(&round) else {
            return false;
        };
        authorities.iter().all(|authority| {
            blocks
                .keys()
                .any(|(block_authority, _)| block_authority == authority)
        })
    }

    pub fn block_exists(&self, reference: BlockReference) -> bool {
        self.inner.read().block_exists(reference)
    }

    pub fn get_transaction(&self, locator: &TransactionLocator) -> Option<Transaction> {
        self.get_block(*locator.block())
            .and_then(|block| {
                block
                    .statements()
                    .get(locator.offset() as usize)
                    .cloned()
                    .map(|statement| {
                        if let BaseStatement::Share(transaction) = statement {
                            Some(transaction)
                        } else {
                            None
                        }
                    })
            })
            .flatten()
    }

    pub fn len_expensive(&self) -> usize {
        let inner = self.inner.read();
        inner.index.values().map(HashMap::len).sum()
    }

    pub fn highest_round(&self) -> RoundNumber {
        self.inner.read().highest_round
    }

    pub fn cleanup(&self, threshold_round: RoundNumber) {
        if threshold_round == 0 {
            return;
        }
        let _timer = self.metrics.block_store_cleanup_util.utilization_timer();
        let unloaded = self.inner.write().unload_below_round(threshold_round);
        self.metrics
            .block_store_unloaded_blocks
            .inc_by(unloaded as u64);
        let retained_maps = self.block_wal_reader.cleanup();
        self.metrics.wal_mappings.set(retained_maps as i64);
    }

    pub fn get_own_blocks(
        &self,
        from_excluded: RoundNumber,
        limit: usize,
    ) -> Vec<Data<StatementBlock>> {
        let entries = self.inner.read().get_own_blocks(from_excluded, limit);
        self.read_index_vec(entries)
    }

    pub fn get_others_blocks(
        &self,
        from_excluded: RoundNumber,
        authority: AuthorityIndex,
        limit: usize,
    ) -> Vec<Data<StatementBlock>> {
        let entries = self
            .inner
            .read()
            .get_others_blocks(from_excluded, authority, limit);
        self.read_index_vec(entries)
    }

    pub fn last_seen_by_authority(&self, authority: AuthorityIndex) -> RoundNumber {
        self.inner.read().last_seen_by_authority(authority)
    }

    pub fn last_own_block_ref(&self) -> Option<BlockReference> {
        self.inner.read().last_own_block()
    }

    fn read_index(&self, entry: IndexEntry) -> Data<StatementBlock> {
        match entry {
            IndexEntry::WalPosition(position) => {
                self.metrics.block_store_loaded_blocks.inc();
                let (tag, data) = self
                    .block_wal_reader
                    .read(position)
                    .expect("Failed to read wal");
                match tag {
                    WAL_ENTRY_BLOCK | WAL_ENTRY_ADVERSARY_BLOCK => {
                        Data::from_bytes(data).expect("Failed to deserialize data from wal")
                    }
                    WAL_ENTRY_OWN_BLOCK => {
                        OwnBlockData::from_bytes(data)
                            .expect("Failed to deserialized own block from wal")
                            .1
                    }
                    _ => {
                        panic!("Trying to load index entry at position {position}, found tag {tag}")
                    }
                }
            }
            IndexEntry::Loaded(_, block) => block,
        }
    }

    fn read_index_vec(&self, entries: Vec<IndexEntry>) -> Vec<Data<StatementBlock>> {
        entries
            .into_iter()
            .map(|pos| self.read_index(pos))
            .collect()
    }

    /// Check whether `earlier_block` is an ancestor of `later_block`.
    pub fn linked(
        &self,
        later_block: &Data<StatementBlock>,
        earlier_block: &Data<StatementBlock>,
    ) -> bool {
        let mut parents = vec![later_block.clone()];
        for r in (earlier_block.round()..later_block.round()).rev() {
            parents = self
                .get_blocks_by_round(r)
                .into_iter()
                .filter(|block| {
                    parents
                        .iter()
                        .any(|x| x.includes().contains(block.reference()))
                })
                .collect();
        }
        parents.contains(earlier_block)
    }
}

impl BlockStoreInner {
    pub fn block_exists(&self, reference: BlockReference) -> bool {
        let Some(blocks) = self.index.get(&reference.round) else {
            return false;
        };
        blocks.contains_key(&(reference.authority, reference.digest))
    }

    pub fn get_blocks_at_authority_round(
        &self,
        authority: AuthorityIndex,
        round: RoundNumber,
    ) -> Vec<IndexEntry> {
        let Some(blocks) = self.index.get(&round) else {
            return vec![];
        };
        blocks
            .iter()
            .filter_map(|((a, _), entry)| {
                if *a == authority {
                    Some(entry.clone())
                } else {
                    None
                }
            })
            .collect()
    }

    pub fn get_blocks_by_round(&self, round: RoundNumber) -> Vec<IndexEntry> {
        let Some(blocks) = self.index.get(&round) else {
            return vec![];
        };
        blocks.values().cloned().collect()
    }

    pub fn get_block(&self, reference: BlockReference) -> Option<IndexEntry> {
        self.index
            .get(&reference.round)?
            .get(&(reference.authority, reference.digest))
            .cloned()
    }

    // todo - also specify LRU criteria
    /// Unload all entries from below or equal threshold_round
    pub fn unload_below_round(&mut self, threshold_round: RoundNumber) -> usize {
        let mut unloaded = 0usize;
        for (round, map) in self.index.iter_mut() {
            // todo - try BTreeMap for self.index?
            if *round > threshold_round {
                continue;
            }
            for entry in map.values_mut() {
                match entry {
                    IndexEntry::WalPosition(_) => {}
                    // Unload entry
                    IndexEntry::Loaded(position, _) => {
                        unloaded += 1;
                        *entry = IndexEntry::WalPosition(*position);
                    }
                }
            }
        }
        if unloaded > 0 {
            tracing::debug!("Unloaded {unloaded} entries from block store cache");
        }
        unloaded
    }

    pub fn add_unloaded(&mut self, reference: &BlockReference, position: WalPosition) {
        self.highest_round = max(self.highest_round, reference.round());
        let map = self.index.entry(reference.round()).or_default();
        map.insert(reference.author_digest(), IndexEntry::WalPosition(position));
        self.add_own_index(reference);
        self.update_last_seen_by_authority(reference);
    }

    pub fn add_loaded(&mut self, position: WalPosition, block: Data<StatementBlock>) {
        self.highest_round = max(self.highest_round, block.round());
        self.add_own_index(block.reference());
        self.update_last_seen_by_authority(block.reference());
        let map = self.index.entry(block.round()).or_default();
        map.insert(
            (block.author(), block.digest()),
            IndexEntry::Loaded(position, block),
        );
    }

    pub fn last_seen_by_authority(&self, authority: AuthorityIndex) -> RoundNumber {
        *self
            .last_seen_by_authority
            .get(authority as usize)
            .expect("last_seen_by_authority not found")
    }

    fn update_last_seen_by_authority(&mut self, reference: &BlockReference) {
        let last_seen = self
            .last_seen_by_authority
            .get_mut(reference.authority as usize)
            .expect("last_seen_by_authority not found");
        if reference.round() > *last_seen {
            *last_seen = reference.round();
        }
    }

    pub fn get_own_blocks(&self, from_excluded: RoundNumber, limit: usize) -> Vec<IndexEntry> {
        self.own_blocks
            .range((from_excluded + 1)..)
            .take(limit)
            .map(|(round, digest)| {
                let reference = BlockReference {
                    authority: self.authority,
                    round: *round,
                    digest: *digest,
                };
                if let Some(block) = self.get_block(reference) {
                    block
                } else {
                    panic!("Own block index corrupted, not found: {reference}");
                }
            })
            .collect()
    }

    pub fn get_others_blocks(
        &self,
        from_excluded: RoundNumber,
        authority: AuthorityIndex,
        limit: usize,
    ) -> Vec<IndexEntry> {
        self.index
            .range((from_excluded + 1)..)
            .take(limit)
            .flat_map(|(round, map)| {
                map.keys()
                    .filter(|(a, _)| *a == authority)
                    .map(|(a, d)| BlockReference {
                        authority: *a,
                        round: *round,
                        digest: *d,
                    })
            })
            .map(|reference| {
                self.get_block(reference)
                    .unwrap_or_else(|| panic!("Block index corrupted, not found: {reference}"))
            })
            .collect()
    }

    fn add_own_index(&mut self, reference: &BlockReference) {
        if reference.authority != self.authority {
            return;
        }
        if reference.round > self.last_own_block.map(|r| r.round).unwrap_or_default() {
            self.last_own_block = Some(*reference);
        }
        // Keep one canonical cursor entry while retaining every digest in index.
        // Core persists the primary proposal before any adversarial alternatives.
        self.own_blocks
            .entry(reference.round)
            .or_insert(reference.digest);
    }

    pub fn last_own_block(&self) -> Option<BlockReference> {
        self.last_own_block
    }
}

pub const WAL_ENTRY_BLOCK: Tag = 1;
pub const WAL_ENTRY_PAYLOAD: Tag = 2;
pub const WAL_ENTRY_OWN_BLOCK: Tag = 3;
pub const WAL_ENTRY_STATE: Tag = 4;
// Commit entry includes both commit interpreter incremental state and committed transactions aggregator
// todo - They could be separated for better performance, but this will require catching up for committed transactions aggregator state
pub const WAL_ENTRY_COMMIT: Tag = 5;
const WAL_ENTRY_ADVERSARY_BLOCK: Tag = 6;

impl BlockWriter for (&mut WalWriter, &BlockStore) {
    fn insert_block(&mut self, block: Data<StatementBlock>) -> WalPosition {
        let pos = self
            .0
            .write(WAL_ENTRY_BLOCK, block.serialized_bytes())
            .expect("Writing to wal failed");
        self.1.insert_block(block, pos);
        pos
    }

    fn insert_own_block(&mut self, data: &OwnBlockData) {
        let block_pos = data.write_to_wal(self.0);
        self.1.insert_block(data.block.clone(), block_pos);
    }
}

// This data structure has a special serialization in/from Bytes, see OwnBlockData::from_bytes/write_to_wal
pub struct OwnBlockData {
    pub next_entry: WalPosition,
    pub block: Data<StatementBlock>,
}

const OWN_BLOCK_HEADER_SIZE: usize = 8;

impl OwnBlockData {
    // A bit of custom serialization to minimize data copy, relies on own_block_serialization_test
    pub fn from_bytes(bytes: Bytes) -> bincode::Result<(OwnBlockData, Data<StatementBlock>)> {
        let next_entry = &bytes[..OWN_BLOCK_HEADER_SIZE];
        let next_entry: WalPosition = bincode::deserialize(next_entry)?;
        let block = bytes.slice(OWN_BLOCK_HEADER_SIZE..);
        let block = Data::<StatementBlock>::from_bytes(block)?;
        let own_block_data = OwnBlockData {
            next_entry,
            block: block.clone(),
        };
        Ok((own_block_data, block))
    }

    pub fn write_to_wal(&self, writer: &mut WalWriter) -> WalPosition {
        let header = bincode::serialize(&self.next_entry).expect("Serialization failed");
        let header = IoSlice::new(&header);
        let block = IoSlice::new(self.block.serialized_bytes());
        writer
            .writev(WAL_ENTRY_OWN_BLOCK, &[header, block])
            .expect("Writing to wal failed")
    }
}

#[derive(Serialize, Deserialize)]
pub struct CommitData {
    pub leader: BlockReference,
    // All committed blocks, including the leader
    pub sub_dag: Vec<BlockReference>,
}

impl From<&CommittedSubDag> for CommitData {
    fn from(value: &CommittedSubDag) -> Self {
        let sub_dag = value.blocks.iter().map(|b| *b.reference()).collect();
        Self {
            leader: value.anchor,
            sub_dag,
        }
    }
}

#[cfg(test)]
mod test {
    use super::*;

    #[test]
    fn own_block_serialization_test() {
        let next_entry = WalPosition::default();
        let serialized = bincode::serialize(&next_entry).unwrap();
        assert_eq!(serialized.len(), OWN_BLOCK_HEADER_SIZE);
    }

    #[test]
    fn own_forks_preserve_primary_cursor_and_survive_wal_replay() {
        crate::crypto::with_real_crypto_for_test(|| {
            check_own_fork_replay();
        });
    }

    fn check_own_fork_replay() {
        use crate::{crypto::dummy_signer, test_util::test_metrics, wal::walf};

        let committee = Committee::new_test(vec![1; 4]);
        let file = tempfile::tempfile().unwrap();
        let recovery_file = file.try_clone().unwrap();
        let (mut writer, reader) = walf(file).unwrap();
        let recovered = BlockStore::open(0, Arc::new(reader), &writer, test_metrics(), &committee);
        let store = recovered.block_store;
        let genesis: Vec<_> = committee
            .authorities()
            .map(|a| *StatementBlock::new_genesis(a).reference())
            .collect();
        let primary = Data::new(StatementBlock::new_with_signer(
            0,
            1,
            genesis.clone(),
            vec![],
            1,
            false,
            &dummy_signer(),
        ));
        let mut fork_parents = genesis;
        fork_parents[1..].reverse();
        let fork = Data::new(StatementBlock::new_with_signer(
            0,
            1,
            fork_parents,
            vec![],
            1,
            false,
            &dummy_signer(),
        ));
        primary.verify(&committee).unwrap();
        fork.verify(&committee).unwrap();
        assert_ne!(primary.reference(), fork.reference());
        store.persist_own_block_with_forks(
            &OwnBlockData {
                next_entry: WalPosition::MAX,
                block: primary.clone(),
            },
            vec![fork.clone()],
            &mut writer,
        );
        assert_eq!(store.get_own_blocks(0, 10), vec![primary.clone()]);
        assert_eq!(store.get_block(*fork.reference()), Some(fork.clone()));
        assert_eq!(store.get_blocks_at_authority_round(0, 1).len(), 2);
        writer.sync().unwrap();
        drop(store);
        drop(writer);

        let (writer, reader) = walf(recovery_file).unwrap();
        let recovered = BlockStore::open(0, Arc::new(reader), &writer, test_metrics(), &committee);
        assert_eq!(recovered.last_own_block.unwrap().block, primary.clone());
        assert_eq!(
            recovered.block_store.get_own_blocks(0, 10),
            vec![primary.clone()]
        );
        assert_eq!(
            recovered.block_store.get_block(*fork.reference()),
            Some(fork)
        );
        // A stored extra fork is not another application proposal during replay.
        assert_eq!(recovered.unprocessed_blocks, vec![primary]);
        assert!(recovered.pending.is_empty());
    }
}
