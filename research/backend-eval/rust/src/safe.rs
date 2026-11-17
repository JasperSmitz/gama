//! The write safety protocol: preflight, backup + journal, temp copy in the same
//! directory, library write, re-read verification, atomic rename, rollback.

use crate::fmt::{self, Op, guarded, sha256_file};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::fs::{self, File, OpenOptions};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};

pub struct WriteArgs {
    op: Op,
    op_name: String,
    backup_dir: PathBuf,
    journal: PathBuf,
    continue_on_error: bool,
    fail_after: Option<usize>,
    files: Vec<PathBuf>,
}

impl WriteArgs {
    pub fn parse(cmd: &str, a: &[String]) -> Result<Self, String> {
        let (mut genres, mut bd, mut j, mut coe, mut fa, mut files) =
            (None, None, None, false, None, vec![]);
        let mut i = 0;
        while i < a.len() {
            let flag = a[i].clone();
            let mut val = || {
                i += 1;
                a.get(i).cloned().ok_or(format!("missing value for {flag}"))
            };
            match flag.as_str() {
                "--genres" => genres = Some(val()?),
                "--backup-dir" => bd = Some(PathBuf::from(val()?)),
                "--journal" => j = Some(PathBuf::from(val()?)),
                "--fail-after" => fa = Some(val()?.parse::<usize>().map_err(|e| e.to_string())?),
                "--continue-on-error" => coe = true,
                s if s.starts_with("--") => return Err(format!("unknown flag {s}")),
                s => files.push(PathBuf::from(s)),
            }
            i += 1;
        }
        let op = if cmd == "set-genres" {
            let g = genres.ok_or("--genres is required")?;
            Op::Genres(
                g.split(';')
                    .filter(|s| !s.is_empty())
                    .map(String::from)
                    .collect(),
            )
        } else {
            Op::Fields
        };
        // set-fields may put its backups and journal in a temp dir.
        let tmp = std::env::temp_dir().join(format!(
            "proto-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let backup_dir = match (bd, cmd) {
            (Some(b), _) => b,
            (None, "set-fields") => tmp.join("backup"),
            _ => return Err("--backup-dir is required".into()),
        };
        let journal = match (j, cmd) {
            (Some(j), _) => j,
            (None, "set-fields") => tmp.join("journal.json"),
            _ => return Err("--journal is required".into()),
        };
        if files.is_empty() {
            return Err("no files given".into());
        }
        Ok(Self {
            op,
            op_name: cmd.into(),
            backup_dir,
            journal,
            continue_on_error: coe,
            fail_after: fa,
            files,
        })
    }
}

#[derive(Serialize, Deserialize, Clone)]
pub struct Entry {
    path: PathBuf,
    backup: PathBuf,
    sha256_before: String,
    sha256_after: Option<String>,
    mode: u32,
    state: String, // backed_up | committed | failed | rolled_back | rollback_failed
    error: Option<String>,
}

#[derive(Serialize, Deserialize)]
pub struct Journal {
    op: String,
    created_unix: u64,
    entries: Vec<Entry>,
}

fn fsync_dir(p: &Path) -> std::io::Result<()> {
    File::open(p)?.sync_all()
}

fn parent(p: &Path) -> PathBuf {
    match p.parent() {
        Some(d) if !d.as_os_str().is_empty() => d.to_path_buf(),
        _ => PathBuf::from("."),
    }
}

/// Durable journal write: temp file, fsync, rename, fsync directory.
fn write_journal(path: &Path, j: &Journal) -> Result<(), String> {
    let tmp = path.with_extension("json.tmp");
    let data = serde_json::to_vec_pretty(j).map_err(|e| e.to_string())?;
    (|| -> std::io::Result<()> {
        let mut f = File::create(&tmp)?;
        std::io::Write::write_all(&mut f, &data)?;
        f.sync_all()?;
        fs::rename(&tmp, path)?;
        fsync_dir(&parent(path))
    })()
    .map_err(|e| format!("journal write: {e}"))
}

/// Removes the temp file on drop unless disarmed (step 8: no temp leftovers).
struct TempGuard(Option<PathBuf>);
impl Drop for TempGuard {
    fn drop(&mut self) {
        if let Some(p) = self.0.take() {
            let _ = fs::remove_file(p);
        }
    }
}

fn temp_name(path: &Path, tag: &str) -> PathBuf {
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    parent(path).join(format!(".{name}.proto-{}-{tag}.tmp", std::process::id()))
}

// ---------------------------------------------------------------- preflight

struct Healthy {
    path: PathBuf,
    ty: lofty::file::FileType,
    mode: u32,
    sha256: String,
}

fn preflight(path: &Path) -> (Value, Option<Healthy>) {
    let mut rep = json!({"path": path.display().to_string()});
    let res = (|| -> Result<Healthy, String> {
        let m = fs::symlink_metadata(path).map_err(|e| format!("stat: {e}"))?;
        if !m.file_type().is_file() {
            return Err("not a regular file".into());
        }
        let mode = m.permissions().mode() & 0o7777;
        rep["mode"] = json!(format!("{mode:o}"));
        // Can the OS open it for writing? (Opening does not modify the file.)
        let os_write = OpenOptions::new().write(true).open(path);
        rep["os_open_for_write"] = json!(os_write.is_ok());
        // Would the temp+rename path work? It only needs directory write access,
        // so it would silently bypass a read-only file mode. Probe it.
        let probe = temp_name(path, "preflight");
        let dir_ok = File::create_new(&probe).is_ok();
        let _ = fs::remove_file(&probe);
        rep["dir_writable_rename_possible"] = json!(dir_ok);
        if let Err(e) = os_write {
            return Err(format!(
                "file not writable ({e}); refusing even though rename-over {} have succeeded",
                if dir_ok { "would" } else { "would not" }
            ));
        }
        if !dir_ok {
            return Err("directory not writable (temp file + rename impossible)".into());
        }
        let p = path.to_path_buf();
        let det = guarded("preflight parse", move || {
            let d = fmt::detect(&p)?;
            fmt::read_concrete_path(&p, d.chosen, true)?;
            Ok(d)
        })?;
        rep["format"] = json!(format!("{:?}", det.chosen));
        rep["detection"] = json!(det.method);
        Ok(Healthy {
            path: path.to_path_buf(),
            ty: det.chosen,
            mode,
            sha256: sha256_file(path).map_err(|e| format!("hash: {e}"))?,
        })
    })();
    match res {
        Ok(h) => {
            rep["ok"] = json!(true);
            (rep, Some(h))
        }
        Err(e) => {
            rep["ok"] = json!(false);
            rep["error"] = json!(e);
            (rep, None)
        }
    }
}

// ---------------------------------------------------------------- per-file write

fn diff(a: &fmt::Snapshot, b: &fmt::Snapshot) -> String {
    let mut out = vec![];
    for x in &a.items {
        if !b.items.contains(x) {
            out.push(format!("expected {}={:?}", x.0, x.1));
        }
    }
    for x in &b.items {
        if !a.items.contains(x) {
            out.push(format!("got {}={:?}", x.0, x.1));
        }
    }
    if out.is_empty() && a.items != b.items {
        out.push("same items, different order".into());
    }
    if a.pictures != b.pictures {
        out.push(format!("pictures {:?} -> {:?}", a.pictures, b.pictures));
    }
    out.truncate(8);
    out.join("; ")
}

/// Steps 3-4: temp copy, library write, fsync, re-read and verify. Returns
/// notes on success. The caller owns the temp guard.
fn write_and_verify(h: &Healthy, temp: &Path, op: &Op) -> Result<Vec<String>, String> {
    fs::copy(&h.path, temp).map_err(|e| format!("copy to temp: {e}"))?;
    fs::set_permissions(temp, fs::Permissions::from_mode(h.mode | 0o600))
        .map_err(|e| format!("chmod temp: {e}"))?;
    let (orig, tmp, ty, op1) = (h.path.clone(), temp.to_path_buf(), h.ty, op.clone());
    guarded("write+verify", move || {
        // expected = pre-read of the original with the edit applied in memory
        let mut expected = fmt::read_concrete_path(&orig, ty, true)?;
        expected.apply(&op1);
        let mut c = fmt::read_concrete_path(&tmp, ty, true)?;
        let mut notes = c.apply(&op1);
        let fixed = c.pre_save_workarounds();
        if !fixed.is_empty() {
            notes.push(format!("lofty workarounds applied: {fixed:?}"));
        }
        let mut f = OpenOptions::new()
            .read(true)
            .write(true)
            .open(&tmp)
            .map_err(|e| format!("open temp: {e}"))?;
        c.save_to(&mut f)?;
        f.sync_all().map_err(|e| format!("fsync temp: {e}"))?;
        drop(f);
        let back = fmt::read_concrete_path(&tmp, ty, true).map_err(|e| format!("re-read: {e}"))?;
        // intended values present
        match &op1 {
            Op::Genres(g) => {
                for (tag, got) in back.all_genre_views() {
                    if &got != g {
                        return Err(format!("verify: {tag} genres {got:?} != {g:?}"));
                    }
                }
            }
            Op::Fields => {
                let v = back.fields_view();
                let want = json!({"title": fmt::F_TITLE, "artist": fmt::F_ARTIST,
                    "album": fmt::F_ALBUM, "album_artist": fmt::F_ALBUM_ARTIST, "year": 2024,
                    "track": 7, "track_total": 12, "disc": 2, "disc_total": 3});
                if v != want {
                    return Err(format!("verify: fields {v} != {want}"));
                }
            }
        }
        // everything else equals the pre-read (+ edit)
        let (es, bs) = (expected.snapshot(), back.snapshot());
        if es != bs {
            return Err(format!("verify: unexpected tag change: {}", diff(&es, &bs)));
        }
        Ok(notes)
    })
}

fn commit_one(
    i: usize,
    h: &Healthy,
    a: &WriteArgs,
    j: &mut Journal,
) -> Result<Vec<String>, String> {
    // Step 2: backup + journal (fsynced) before touching anything.
    let sha_now = sha256_file(&h.path).map_err(|e| format!("hash: {e}"))?;
    if sha_now != h.sha256 {
        return Err("file changed since preflight".into());
    }
    let name = h.path.file_name().unwrap_or_default().to_string_lossy();
    let backup = a.backup_dir.join(format!("{i:04}-{name}"));
    fs::copy(&h.path, &backup).map_err(|e| format!("backup copy: {e}"))?;
    File::open(&backup)
        .and_then(|f| f.sync_all())
        .map_err(|e| format!("backup fsync: {e}"))?;
    if sha256_file(&backup).map_err(|e| e.to_string())? != h.sha256 {
        return Err("backup sha256 mismatch".into());
    }
    fsync_dir(&a.backup_dir).map_err(|e| format!("backup dir fsync: {e}"))?;
    j.entries.push(Entry {
        path: fs::canonicalize(&h.path).unwrap_or(h.path.clone()),
        backup: fs::canonicalize(&backup).unwrap_or(backup),
        sha256_before: h.sha256.clone(),
        sha256_after: None,
        mode: h.mode,
        state: "backed_up".into(),
        error: None,
    });
    write_journal(&a.journal, j)?;

    // Steps 3-4.
    let temp = temp_name(&h.path, &i.to_string());
    let mut guard = TempGuard(Some(temp.clone()));
    let notes = write_and_verify(h, &temp, &a.op)?;

    // Step 5: restore mode, atomic rename, fsync dir, journal.
    fs::set_permissions(&temp, fs::Permissions::from_mode(h.mode))
        .map_err(|e| format!("chmod: {e}"))?;
    fs::rename(&temp, &h.path).map_err(|e| format!("rename: {e}"))?;
    guard.0 = None;
    fsync_dir(&parent(&h.path)).map_err(|e| format!("dir fsync: {e}"))?;
    let e = j.entries.last_mut().unwrap();
    e.state = "committed".into();
    e.sha256_after = sha256_file(&h.path).ok();
    write_journal(&a.journal, j)?;
    Ok(notes)
}

// ---------------------------------------------------------------- rollback

/// Restore one file from its backup via temp + rename, then check sha256.
fn restore(e: &Entry) -> Result<&'static str, String> {
    if sha256_file(&e.path).ok().as_deref() == Some(e.sha256_before.as_str()) {
        return Ok("already_original");
    }
    let temp = temp_name(&e.path, "restore");
    let mut guard = TempGuard(Some(temp.clone()));
    fs::copy(&e.backup, &temp).map_err(|x| format!("copy backup: {x}"))?;
    File::open(&temp)
        .and_then(|f| f.sync_all())
        .map_err(|x| format!("fsync: {x}"))?;
    fs::set_permissions(&temp, fs::Permissions::from_mode(e.mode))
        .map_err(|x| format!("chmod: {x}"))?;
    fs::rename(&temp, &e.path).map_err(|x| format!("rename: {x}"))?;
    guard.0 = None;
    fsync_dir(&parent(&e.path)).map_err(|x| format!("dir fsync: {x}"))?;
    let got = sha256_file(&e.path).map_err(|x| x.to_string())?;
    if got != e.sha256_before {
        return Err(format!("restored sha256 {got} != {}", e.sha256_before));
    }
    Ok("restored")
}

fn rollback_entries(j: &mut Journal, only_committed: bool) -> Vec<Value> {
    let mut out = vec![];
    for e in j.entries.iter_mut().rev() {
        if only_committed && e.state != "committed" {
            continue;
        }
        let r = restore(e);
        e.state = if r.is_ok() {
            "rolled_back"
        } else {
            "rollback_failed"
        }
        .into();
        out.push(
            json!({"path": e.path, "result": r.as_ref().ok(), "error": r.err(),
                        "sha256_verified": e.state == "rolled_back"}),
        );
    }
    out
}

pub fn cmd_rollback(journal: &Path) -> i32 {
    let mut j: Journal = match fs::read(journal)
        .map_err(|e| e.to_string())
        .and_then(|b| serde_json::from_slice(&b).map_err(|e| e.to_string()))
    {
        Ok(j) => j,
        Err(e) => {
            crate::print(&json!({"ok": false, "error": format!("journal: {e}")}));
            return 1;
        }
    };
    let res = rollback_entries(&mut j, false);
    let ok = j.entries.iter().all(|e| e.state == "rolled_back");
    let jw = write_journal(journal, &j);
    crate::print(&json!({"ok": ok && jw.is_ok(), "restored": res, "journal_error": jw.err()}));
    if ok { 0 } else { 1 }
}

// ---------------------------------------------------------------- batch

pub fn cmd_write(a: WriteArgs) -> i32 {
    // Step 1: preflight everything before touching anything.
    let mut pre = vec![];
    let mut healthy = vec![];
    for f in &a.files {
        let (rep, h) = preflight(f);
        pre.push(rep);
        if let Some(h) = h {
            healthy.push(h);
        }
    }
    let bad = a.files.len() - healthy.len();
    if bad > 0 && !a.continue_on_error {
        crate::print(&json!({"ok": false, "op": a.op_name,
            "error": format!("preflight failed for {bad} file(s); batch aborted, nothing modified"),
            "preflight": pre, "results": []}));
        return 1;
    }
    if let Err(e) = fs::create_dir_all(&a.backup_dir) {
        crate::print(&json!({"ok": false, "error": format!("backup dir: {e}")}));
        return 1;
    }
    if let Some(d) = a.journal.parent().filter(|d| !d.as_os_str().is_empty()) {
        let _ = fs::create_dir_all(d);
    }
    let mut j = Journal {
        op: format!("{} {:?}", a.op_name, a.op),
        created_unix: std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs(),
        entries: vec![],
    };
    if let Err(e) = write_journal(&a.journal, &j) {
        crate::print(&json!({"ok": false, "error": e}));
        return 1;
    }

    let mut results = vec![];
    let mut committed = 0usize;
    let mut fatal: Option<String> = None;
    for (i, h) in healthy.iter().enumerate() {
        if a.fail_after == Some(committed) {
            // Simulated crash/error: always fatal for the batch (step 6 test).
            fatal = Some(format!("simulated failure after {committed} commit(s)"));
            break;
        }
        match commit_one(i, h, &a, &mut j) {
            Ok(notes) => {
                committed += 1;
                results.push(json!({"path": h.path, "status": "committed", "notes": notes}));
            }
            Err(e) => {
                if let Some(en) = j.entries.last_mut().filter(|en| {
                    en.path.ends_with(h.path.file_name().unwrap_or_default())
                        && en.state == "backed_up"
                }) {
                    en.state = "failed".into();
                    en.error = Some(e.clone());
                }
                let _ = write_journal(&a.journal, &j);
                results.push(json!({"path": h.path, "status": "failed", "error": e}));
                if !a.continue_on_error {
                    fatal = Some(format!("{}: {e}", h.path.display()));
                    break;
                }
            }
        }
    }
    let rolled_back = if fatal.is_some() {
        let r = rollback_entries(&mut j, true);
        let _ = write_journal(&a.journal, &j);
        r
    } else {
        vec![]
    };
    let failures = results.iter().filter(|r| r["status"] == "failed").count();
    let ok = fatal.is_none() && failures == 0 && bad == 0;
    crate::print(&json!({
        "ok": ok, "op": a.op_name, "fatal": fatal, "committed": committed,
        "failed": failures, "preflight_skipped": bad, "journal": a.journal,
        "backup_dir": a.backup_dir, "preflight": pre, "results": results,
        "rolled_back": rolled_back,
    }));
    if ok { 0 } else { 1 }
}
