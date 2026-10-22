//! Thread-safe record of what the worker is doing, read by the window and tray.

use std::collections::VecDeque;
use std::sync::Mutex;
use std::time::Instant;

use chrono::{DateTime, Local};

use crate::state::QueueOrder;

#[derive(Debug, Clone, Default)]
pub struct QueueItem {
    pub podcast: String,
    pub title: String,
    pub folder: String,
    pub guid: String,
    pub published: String,
    /// auto | requested | reanalyze
    pub source: String,
}

impl QueueItem {
    pub fn key(&self) -> String {
        format!("{}|{}", self.folder, self.guid)
    }
}

#[derive(Debug, Clone)]
pub struct Current {
    pub item: QueueItem,
    pub step: String,
    pub pct: Option<i32>,
    pub started: Instant,
}

#[derive(Debug, Clone)]
pub struct Finished {
    pub time: DateTime<Local>,
    pub item: QueueItem,
    /// done | failed | stopped | paused
    pub result: String,
    pub detail: String,
    pub seconds: f64,
}

#[derive(Debug, Clone, Default)]
pub struct Snapshot {
    pub current: Option<Current>,
    pub queue: Vec<QueueItem>,
    pub recent: Vec<Finished>,
    pub checking: String,
    pub cycle_running: bool,
    pub last_check: Option<DateTime<Local>>,
}

#[derive(Default)]
pub struct Activity {
    inner: Mutex<Snapshot>,
}

impl Activity {
    pub fn cycle_started(&self) {
        let mut s = self.inner.lock().unwrap();
        s.cycle_running = true;
        s.queue.clear();
        s.current = None;
    }

    pub fn cycle_finished(&self) {
        let mut s = self.inner.lock().unwrap();
        s.cycle_running = false;
        s.queue.clear();
        s.current = None;
        s.checking.clear();
        s.last_check = Some(Local::now());
    }

    pub fn set_checking(&self, podcast: &str) {
        self.inner.lock().unwrap().checking = podcast.into();
    }

    pub fn set_queue(&self, items: Vec<QueueItem>) {
        let mut s = self.inner.lock().unwrap();
        s.checking.clear();
        s.queue = items;
    }

    pub fn start_job(&self, item: &QueueItem) {
        let mut s = self.inner.lock().unwrap();
        s.queue.retain(|q| !(q.folder == item.folder && q.guid == item.guid));
        s.current = Some(Current { item: item.clone(), step: "starting".into(), pct: None, started: Instant::now() });
    }

    pub fn set_step(&self, step: &str, pct: Option<i32>) {
        let mut s = self.inner.lock().unwrap();
        if let Some(c) = s.current.as_mut() {
            c.step = step.into();
            c.pct = pct;
        }
    }

    pub fn finish_job(&self, result: &str, detail: &str) {
        let mut s = self.inner.lock().unwrap();
        if let Some(c) = s.current.take() {
            s.recent.insert(0, Finished {
                time: Local::now(),
                item: c.item,
                result: result.into(),
                detail: detail.into(),
                seconds: c.started.elapsed().as_secs_f64(),
            });
            s.recent.truncate(100);
        }
    }

    /// Put the waiting queue in the given order ("folder|guid" keys).
    pub fn reorder(&self, keys: &[String]) {
        let mut s = self.inner.lock().unwrap();
        let q = std::mem::take(&mut s.queue);
        s.queue = QueueOrder::sort(q, |q| q.key(), keys, |_| false);
    }

    pub fn drop_queued(&self, folder: &str, guid: &str) {
        self.inner.lock().unwrap().queue.retain(|q| !(q.folder == folder && q.guid == guid));
    }

    pub fn has_current(&self) -> bool {
        self.inner.lock().unwrap().current.is_some()
    }

    pub fn snapshot(&self) -> Snapshot {
        self.inner.lock().unwrap().clone()
    }
}

#[allow(dead_code)]
fn _assert_send_sync() {
    fn f<T: Send + Sync>() {}
    f::<Activity>();
    let _ = VecDeque::<u8>::new();
}
