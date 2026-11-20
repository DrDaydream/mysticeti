//! Benchmark timing is separate from the adversary's message-queue clock.
use parking_lot::Mutex;
use serde::{Deserialize, Serialize};
use std::{path::PathBuf, sync::Arc, time::{Duration, Instant}};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub(crate) struct Plan {
    pub schema: u64,
    pub run_id: String,
    pub load_ms: u64,
    pub start_ms: u64,
    pub end_ms: u64,
}

impl Plan {
    fn contains(&self, now: Duration) -> bool {
        now >= Duration::from_millis(self.start_ms) && now < Duration::from_millis(self.end_ms)
    }
}

#[derive(Serialize)]
struct Receipt<'a> {
    #[serde(flatten)]
    plan: &'a Plan,
    authority: u64,
}

#[derive(Serialize)]
struct Window<'a> {
    #[serde(flatten)]
    receipt: Receipt<'a>,
    complete: bool,
    count: u64,
    sum_s: f64,
    input_lateness_ms: Option<u64>,
}

struct Armed { plan: Plan, unix: Duration, mono: Instant }
impl Armed { fn now(&self) -> Duration { self.unix + self.mono.elapsed() } }

#[derive(Default)]
struct State {
    armed: Option<Armed>,
    pending: u64,
    count: u64,
    sum_s: f64,
    late: Option<u64>,
    failed: bool,
}

pub(crate) struct Session {
    directory: Option<PathBuf>,
    run_id: String,
    state: Mutex<State>,
}

impl Session {
    pub fn from_env() -> Arc<Self> {
        let directory = std::env::var_os("BENCHMARK_CONTROL_DIR").map(PathBuf::from);
        let run_id = if directory.is_some() {
            std::env::var("BENCHMARK_RUN_ID").expect("BENCHMARK_RUN_ID is required")
        } else { String::new() };
        Arc::new(Self { directory, run_id, state: Mutex::new(State::default()) })
    }
    pub fn enabled(&self) -> bool { self.directory.is_some() }
    pub fn elapsed(&self) -> Option<Duration> {
        let state = self.state.lock();
        let a = state.armed.as_ref()?;
        a.now().checked_sub(Duration::from_millis(a.plan.start_ms))
    }
    pub fn ended(&self) -> bool {
        let state = self.state.lock();
        state.armed.as_ref().map_or(false, |a| a.now() >= Duration::from_millis(a.plan.end_ms))
    }
    pub async fn wait_input(&self) {
        if !self.enabled() { return; }
        loop {
            {
                let mut state = self.state.lock();
                if let Some(a) = &state.armed {
                    if let Some(late) = a.now().checked_sub(Duration::from_millis(a.plan.load_ms)) {
                        state.late = Some(late.as_millis() as u64);
                        return;
                    }
                }
            }
            crate::runtime::sleep(Duration::from_millis(10)).await;
        }
    }
    pub async fn wait_end(&self) {
        loop {
            if self.ended() { return; }
            crate::runtime::sleep(Duration::from_millis(10)).await;
        }
    }
    fn write<T: Serialize>(&self, name: &str, value: &T) -> eyre::Result<()> {
        let dir = self.directory.as_ref().expect("enabled session");
        let temp = dir.join(format!("{name}.tmp"));
        std::fs::write(&temp, serde_yaml::to_string(value)?)?;
        std::fs::rename(temp, dir.join(name))?;
        Ok(())
    }
    pub async fn control(&self, authority: u64) -> eyre::Result<()> {
        if !self.enabled() { return Ok(()); }
        let path = self.directory.as_ref().unwrap().join("start.yaml");
        let deadline = Instant::now() + Duration::from_secs(240);
        let plan: Plan = loop {
            match std::fs::read_to_string(&path) {
                Ok(text) => break serde_yaml::from_str(&text)?,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound && Instant::now() < deadline => {},
                Err(e) => return Err(e.into()),
            }
            crate::runtime::sleep(Duration::from_millis(20)).await;
        };
        let unix = crate::runtime::timestamp_utc();
        let mono = Instant::now();
        eyre::ensure!(plan.schema == 2 && plan.run_id == self.run_id, "Wrong benchmark identity");
        eyre::ensure!(plan.load_ms <= plan.start_ms && plan.start_ms < plan.end_ms
            && plan.end_ms < (1u64 << 53), "Invalid benchmark interval");
        eyre::ensure!(Duration::from_millis(plan.load_ms) > unix + Duration::from_secs(5), "Expired start plan");
        self.state.lock().armed = Some(Armed { plan: plan.clone(), unix, mono });
        self.write("armed.yaml", &Receipt { plan: &plan, authority })?;
        tracing::info!(event="benchmark_armed", authority, ?plan);
        self.wait_end().await;
        loop {
            {
                let state = self.state.lock();
                eyre::ensure!(!state.failed, "Incomplete commit batch");
                if state.pending == 0 {
                    eyre::ensure!(state.late.map_or(false, |late| late <= 500), "Input start absent or late");
                    self.write("window.yaml", &Window {
                        receipt: Receipt { plan: &plan, authority }, complete: true,
                        count: state.count, sum_s: state.sum_s, input_lateness_ms: state.late,
                    })?;
                    tracing::info!(event="benchmark_window_complete", authority, count=state.count, sum_s=state.sum_s);
                    return Ok(());
                }
            }
            crate::runtime::sleep(Duration::from_millis(10)).await;
        }
    }
    pub fn begin(self: &Arc<Self>) -> Batch {
        let timestamp = crate::runtime::timestamp_utc();
        let mut state = self.state.lock();
        let active = state.armed.as_ref().map_or(false, |a| a.plan.contains(a.now()));
        if active { state.pending += 1; }
        Batch { session: self.clone(), timestamp, active, done: false, count: 0, sum_s: 0.0 }
    }
}

pub(crate) struct Batch {
    session: Arc<Session>,
    pub timestamp: Duration,
    active: bool,
    done: bool,
    count: u64,
    sum_s: f64,
}
impl Batch {
    pub fn observe(&mut self, latency: Duration) {
        if self.active { self.count += 1; self.sum_s += latency.as_secs_f64(); }
    }
    pub fn finish(mut self) {
        if self.active {
            let mut state = self.session.state.lock();
            state.count += self.count;
            state.sum_s += self.sum_s;
            state.pending -= 1;
        }
        self.done = true;
    }
}
impl Drop for Batch {
    fn drop(&mut self) {
        if self.active && !self.done {
            let mut state = self.session.state.lock();
            state.failed = true;
            state.pending -= 1;
        }
    }
}

#[cfg(all(test, not(feature = "simulator")))]
mod tests {
    use super::*;
    fn plan() -> Plan { Plan { schema: 2, run_id: "test".into(), load_ms: 100000, start_ms: 100000, end_ms: 160000 } }
    #[test]
    fn half_open_window_and_no_samples_before_start() {
        let p = plan();
        for (ms, expected) in [(99999,false), (100000,true), (159999,true), (160000,false)] {
            assert_eq!(p.contains(Duration::from_millis(ms)), expected);
        }
        let s = Arc::new(Session { directory: None, run_id: "test".into(), state: Mutex::new(State::default()) });
        assert!(s.elapsed().is_none());
        let mut batch = s.begin();
        batch.observe(Duration::from_secs(5));
        batch.finish();
        assert_eq!(s.state.lock().count, 0);
    }
    #[test]
    fn batch_started_inside_window_finishes_atomically_even_after_end() {
        let s = Arc::new(Session { directory: None, run_id: "test".into(), state: Mutex::new(State {
            armed: Some(Armed { plan: plan(), unix: Duration::from_secs(110), mono: Instant::now() }),
            ..State::default()
        }) });
        let mut batch = s.begin();
        batch.observe(Duration::from_secs(2));
        batch.observe(Duration::from_secs(3));
        assert_eq!(s.state.lock().pending, 1);
        s.state.lock().armed.as_mut().unwrap().unix = Duration::from_secs(170);
        batch.finish();
        let mut outside = s.begin();
        outside.observe(Duration::from_secs(99));
        outside.finish();
        let state = s.state.lock();
        assert_eq!((state.count, state.sum_s, state.pending), (2,5.0,0));
    }
}
