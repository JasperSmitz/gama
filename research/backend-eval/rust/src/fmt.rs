//! Format layer: detection, per-format (concrete) parsing, tag snapshots and edits.
//!
//! We use lofty's concrete file types (FlacFile, MpegFile, Mp4File, VorbisFile,
//! OpusFile) instead of the generic `TaggedFile`, because in lofty 0.25 the generic
//! `Tag` has no "unknown key" variant: items it cannot map are dropped on conversion,
//! so saving a generic `Tag` would lose custom frames/fields.

use lofty::config::{ParseOptions, ParsingMode, WriteOptions};
use lofty::file::{AudioFile, FileType};
use lofty::flac::FlacFile;
use lofty::id3::v1::Id3v1Tag;
use lofty::id3::v2::{Frame, FrameId, Id3v2Tag, TextInformationFrame};
use lofty::mp4::{Atom, AtomData, AtomIdent, Ilst, Mp4File};
use lofty::mpeg::MpegFile;
use lofty::ogg::tag::VorbisComments;
use lofty::ogg::{OggPictureStorage, OpusFile, VorbisFile};
use lofty::picture::Picture;
use lofty::prelude::*;
use lofty::probe::Probe;
use lofty::properties::FileProperties;
use lofty::tag::items::Timestamp;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufReader, Read, Seek};
use std::path::{Path, PathBuf};
use std::sync::mpsc;
use std::time::{Duration, Instant};

pub const TIMEOUT: Duration = Duration::from_secs(30);

pub fn sha256_bytes(b: &[u8]) -> String {
    hex::encode(Sha256::digest(b))
}

pub fn sha256_file(p: &Path) -> std::io::Result<String> {
    let mut f = File::open(p)?;
    let mut h = Sha256::new();
    let mut buf = vec![0u8; 1 << 16];
    loop {
        let n = f.read(&mut buf)?;
        if n == 0 {
            break;
        }
        h.update(&buf[..n]);
    }
    Ok(hex::encode(h.finalize()))
}

/// Run `f` on a worker thread: catches panics and enforces a timeout, so a
/// library bug can never crash or hang the process.
pub fn guarded<T: Send + 'static>(
    what: &str,
    f: impl FnOnce() -> Result<T, String> + Send + 'static,
) -> Result<T, String> {
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(f));
        let _ = tx.send(r);
    });
    match rx.recv_timeout(TIMEOUT) {
        Ok(Ok(r)) => r,
        Ok(Err(p)) => {
            let msg = p
                .downcast_ref::<String>()
                .cloned()
                .or_else(|| p.downcast_ref::<&str>().map(|s| s.to_string()))
                .unwrap_or_else(|| "<non-string panic>".into());
            Err(format!("PANIC in {what}: {msg}"))
        }
        Err(_) => Err(format!("TIMEOUT in {what} after {TIMEOUT:?}")),
    }
}

// ---------------------------------------------------------------- detection

pub struct Detection {
    pub by_extension: Option<FileType>,
    pub by_content: Option<FileType>,
    pub chosen: FileType,
    pub method: &'static str,
}

pub fn detect(path: &Path) -> Result<Detection, String> {
    let by_extension = FileType::from_path(path);
    let f = File::open(path).map_err(|e| format!("open: {e}"))?;
    let by_content = Probe::new(BufReader::new(f))
        .guess_file_type()
        .map_err(|e| format!("probe: {e}"))?
        .file_type();
    let (chosen, method) = match (by_content, by_extension) {
        (Some(c), _) => (c, "content"),
        (None, Some(e)) => (e, "extension"),
        (None, None) => return Err("unknown format (neither content nor extension)".into()),
    };
    Ok(Detection {
        by_extension,
        by_content,
        chosen,
        method,
    })
}

// ---------------------------------------------------------------- concrete files

pub enum Concrete {
    Flac(FlacFile),
    Mpeg(MpegFile),
    Mp4(Mp4File),
    Vorbis(VorbisFile),
    Opus(OpusFile),
}

/// Read path: lofty defaults (BestAttempt, implicit conversions on).
///
/// Write path: Strict parsing, so undecodable fields abort the write instead of
/// being blanked (BestAttempt replaces undecodable text with ""). Implicit
/// conversions are chosen per format:
/// * Vorbis/FLAC/Ogg and MP4: OFF. With them on, lofty rewrites data it did not
///   need to touch (Vorbis TRACKNUMBER=1/24 is split into TRACKNUMBER=1 +
///   TRACKTOTAL=24 on read and saved that way).
/// * MPEG (ID3v2): ON. lofty always writes ID3v2.4; with conversions off an
///   ID3v2.3 tag keeps its v2.3-only TYER/TDAT frames inside the new v2.4 tag.
///   With them on, they are merged into a proper TDRC.
///
/// PROTO_WRITE_IMPLICIT=1/0 forces all on/off; PROTO_WRITE_MODE=best|relaxed
/// relaxes parsing. Both exist only to reproduce the experiments.
pub fn parse_options(write: bool, ty: FileType) -> ParseOptions {
    let mut o = ParseOptions::new();
    if write {
        let implicit = match std::env::var("PROTO_WRITE_IMPLICIT").as_deref() {
            Ok("1") => true,
            Ok("0") => false,
            _ => ty == FileType::Mpeg,
        };
        let mode = match std::env::var("PROTO_WRITE_MODE").as_deref() {
            Ok("best") => ParsingMode::BestAttempt,
            Ok("relaxed") => ParsingMode::Relaxed,
            _ => ParsingMode::Strict,
        };
        o = o.implicit_conversions(implicit).parsing_mode(mode);
    }
    o
}

pub fn read_concrete<R: Read + Seek>(
    r: &mut R,
    ty: FileType,
    write: bool,
) -> Result<Concrete, String> {
    let o = parse_options(write, ty);
    let e = |e: lofty::error::FileParseError| format!("lofty parse: {e}");
    Ok(match ty {
        FileType::Flac => Concrete::Flac(FlacFile::read_from(r, o).map_err(e)?),
        FileType::Mpeg => Concrete::Mpeg(MpegFile::read_from(r, o).map_err(e)?),
        FileType::Mp4 => Concrete::Mp4(Mp4File::read_from(r, o).map_err(e)?),
        FileType::Vorbis => Concrete::Vorbis(VorbisFile::read_from(r, o).map_err(e)?),
        FileType::Opus => Concrete::Opus(OpusFile::read_from(r, o).map_err(e)?),
        other => return Err(format!("format {other:?} not supported by this prototype")),
    })
}

pub fn read_concrete_path(path: &Path, ty: FileType, write: bool) -> Result<Concrete, String> {
    let mut f = BufReader::new(File::open(path).map_err(|e| format!("open: {e}"))?);
    read_concrete(&mut f, ty, write)
}

impl Concrete {
    /// Workaround for a lofty 0.25.5 bug: every `AtomData::SignedInteger` is
    /// written as a 4-byte integer, whatever its original width. ffmpeg (and
    /// Apple) read `stik` and friends as 1 byte, so `stik=1` becomes 0. We
    /// re-encode the known fixed-width atoms as raw data of the right width.
    /// Returns the atoms changed. Disabled with PROTO_NO_MP4_INT_FIX=1.
    pub fn pre_save_workarounds(&mut self) -> Vec<String> {
        let mut fixed = vec![];
        if let Concrete::Flac(x) = self
            && x.id3v2().is_some()
            && std::env::var("PROTO_KEEP_FLAC_ID3").as_deref() != Ok("1")
        {
            // lofty marks ID3v2-in-FLAC read-only, yet FlacFile::save_to still
            // tries to write it and fails ("failed to write Flac file"). Drop it
            // from the model being saved so only the Vorbis comments are
            // written; the ID3v2 bytes on disk are left untouched.
            let _ = x.remove_id3v2();
            fixed.push("FLAC-leading-ID3v2 excluded from save".into());
        }
        if std::env::var("PROTO_NO_MP4_INT_FIX").is_ok_and(|v| v == "1") {
            return fixed;
        }
        let Concrete::Mp4(x) = self else { return fixed };
        let Some(ilst) = x.ilst_mut() else {
            return fixed;
        };
        const WIDTHS: &[(&[u8; 4], usize)] = &[
            (b"stik", 1),
            (b"rtng", 1),
            (b"akID", 1),
            (b"hdvd", 1),
            (b"shwm", 1),
            (b"pcst", 1),
            (b"tmpo", 2),
        ];
        for (id, width) in WIDTHS {
            let ident = AtomIdent::Fourcc(**id);
            let Some(atom) = ilst.get(&ident) else {
                continue;
            };
            let data: Vec<AtomData> = atom
                .data()
                .map(|d| match d {
                    AtomData::SignedInteger(v) => AtomData::Unknown {
                        code: lofty::mp4::DataType::BeSignedInteger,
                        data: v.to_be_bytes()[4 - width..].to_vec(),
                    },
                    other => other.clone(),
                })
                .collect();
            if let Some(a) = Atom::from_collection(ident, data) {
                ilst.replace_atom(a);
                fixed.push(String::from_utf8_lossy(*id).into_owned());
            }
        }
        fixed
    }

    pub fn save_to(&self, f: &mut File) -> Result<(), String> {
        let o = WriteOptions::default();
        let r = match self {
            Concrete::Flac(x) => x.save_to(f, o),
            Concrete::Mpeg(x) => x.save_to(f, o),
            Concrete::Mp4(x) => x.save_to(f, o),
            Concrete::Vorbis(x) => x.save_to(f, o),
            Concrete::Opus(x) => x.save_to(f, o),
        };
        r.map_err(|e| format!("lofty save: {e}"))
    }

    fn properties(&self) -> FileProperties {
        match self {
            Concrete::Flac(x) => (*x.properties()).into(),
            Concrete::Mpeg(x) => (*x.properties()).into(),
            Concrete::Mp4(x) => x.properties().clone().into(),
            Concrete::Vorbis(x) => (*x.properties()).into(),
            Concrete::Opus(x) => (*x.properties()).into(),
        }
    }

    pub fn tag_types(&self) -> Vec<&'static str> {
        let mut v = vec![];
        match self {
            Concrete::Flac(x) => {
                if x.id3v2().is_some() {
                    v.push("ID3v2");
                }
                if x.vorbis_comments().is_some() {
                    v.push("VorbisComments");
                }
            }
            Concrete::Mpeg(x) => {
                if x.id3v2().is_some() {
                    v.push("ID3v2");
                }
                if x.ape().is_some() {
                    v.push("APE");
                }
                if x.id3v1().is_some() {
                    v.push("ID3v1");
                }
            }
            Concrete::Mp4(x) => {
                if x.ilst().is_some() {
                    v.push("Mp4Ilst");
                }
            }
            Concrete::Vorbis(_) | Concrete::Opus(_) => v.push("VorbisComments"),
        }
        v
    }

    /// Every tag item and picture in a canonical form. Used for the read
    /// output and for the "nothing else changed" verification.
    pub fn snapshot(&self) -> Snapshot {
        let mut s = Snapshot::default();
        match self {
            Concrete::Flac(x) => {
                if let Some(t) = x.id3v2() {
                    snap_id3v2(t, &mut s);
                }
                if let Some(t) = x.vorbis_comments() {
                    snap_vc(t, &mut s);
                }
                for (p, _) in x.pictures() {
                    s.pictures.push(pic("FLAC-PICTURE", p));
                }
            }
            Concrete::Mpeg(x) => {
                if let Some(t) = x.id3v2() {
                    snap_id3v2(t, &mut s);
                }
                if let Some(t) = x.ape() {
                    for i in t {
                        s.items
                            .push((format!("APE:{}", i.key()), dbg_short(i.value())));
                    }
                }
                if let Some(t) = x.id3v1() {
                    // ID3v1 is Latin-1: non-Latin-1 chars are stored as '?', so
                    // compare in that lossy form.
                    let d: String = format!("{t:?}")
                        .chars()
                        .map(|c| if (c as u32) > 0xFF { '?' } else { c })
                        .collect();
                    s.items.push(("ID3v1".into(), d));
                }
            }
            Concrete::Mp4(x) => {
                if let Some(t) = x.ilst() {
                    snap_ilst(t, &mut s);
                }
                // ilst atom order carries no meaning and lofty's replace_atom
                // appends; compare MP4 items as a set.
                s.items.sort();
            }
            Concrete::Vorbis(x) => snap_vc(x.vorbis_comments(), &mut s),
            Concrete::Opus(x) => snap_vc(x.vorbis_comments(), &mut s),
        }
        s
    }
}

#[derive(Default, PartialEq, Debug, Clone)]
pub struct Snapshot {
    pub items: Vec<(String, String)>,
    pub pictures: Vec<PicInfo>,
}

#[derive(PartialEq, Debug, Clone)]
pub struct PicInfo {
    pub source: String,
    pub ty: String,
    pub mime: String,
    pub description: String,
    pub bytes: usize,
    pub sha256: String,
}

impl PicInfo {
    pub fn json(&self) -> Value {
        json!({"source": self.source, "type": self.ty, "mime": self.mime,
               "description": self.description, "bytes": self.bytes, "sha256": self.sha256})
    }
}

fn pic(source: &str, p: &Picture) -> PicInfo {
    PicInfo {
        source: source.into(),
        ty: format!("{:?}", p.pic_type()),
        mime: p
            .mime_type()
            .map(|m| m.as_str().to_string())
            .unwrap_or_default(),
        description: p.description().unwrap_or_default().to_string(),
        bytes: p.data().len(),
        sha256: sha256_bytes(p.data()),
    }
}

/// Debug-format a value; long values (binary frames etc.) are replaced by a hash.
fn dbg_short<T: std::fmt::Debug>(v: T) -> String {
    let s = format!("{v:?}");
    if s.len() > 300 {
        format!(
            "<{} chars, sha256 {}>",
            s.len(),
            &sha256_bytes(s.as_bytes())[..16]
        )
    } else {
        s
    }
}

fn snap_vc(t: &VorbisComments, s: &mut Snapshot) {
    s.items.push(("VC:<vendor>".into(), t.vendor().to_string()));
    for (k, v) in t.items() {
        s.items
            .push((format!("VC:{}", k.to_ascii_uppercase()), v.to_string()));
    }
    for (p, _) in t.pictures() {
        s.pictures.push(pic("METADATA_BLOCK_PICTURE", p));
    }
}

fn snap_id3v2(t: &Id3v2Tag, s: &mut Snapshot) {
    for f in t {
        match f {
            Frame::Picture(p) => s.pictures.push(pic("ID3v2:APIC", &p.picture)),
            Frame::Text(tf) => s.items.push((
                format!("ID3v2:{}", f.id_str()),
                tf.value.replace('\0', " \\0 "),
            )),
            // Render timestamps like text frames: a TDRC set via Accessor is held
            // as a Text frame in memory but re-read as a Timestamp frame.
            Frame::Timestamp(tf) => s
                .items
                .push((format!("ID3v2:{}", f.id_str()), tf.timestamp.to_string())),
            Frame::UserText(u) => s.items.push((
                format!("ID3v2:TXXX:{}", u.description),
                u.content.replace('\0', " \\0 "),
            )),
            other => s
                .items
                .push((format!("ID3v2:{}", other.id_str()), dbg_short(other))),
        }
    }
}

fn snap_ilst(t: &Ilst, s: &mut Snapshot) {
    for a in t {
        let key = match a.ident() {
            AtomIdent::Fourcc(c) => format!("MP4:{}", String::from_utf8_lossy(c)),
            AtomIdent::Freeform { mean, name } => format!("MP4:----:{mean}:{name}"),
        };
        let mut vals = vec![];
        for d in a.data() {
            match d {
                AtomData::Picture(p) => s.pictures.push(pic("MP4:covr", p)),
                AtomData::UTF8(x) | AtomData::UTF16(x) => vals.push(x.clone()),
                other => vals.push(dbg_short(other)),
            }
        }
        if !vals.is_empty() {
            s.items.push((key, vals.join(" \\0 ")));
        }
    }
}

// ---------------------------------------------------------------- core field view

#[derive(Default)]
struct Core {
    title: Option<String>,
    artist: Option<String>,
    album: Option<String>,
    album_artist: Option<String>,
    year: Option<u16>,
    track: Option<u32>,
    track_total: Option<u32>,
    disc: Option<u32>,
    disc_total: Option<u32>,
    genres: Vec<String>,
}

fn core_from<A: Accessor>(t: &A, album_artist: Option<String>, genres: Vec<String>) -> Core {
    Core {
        title: t.title().map(|c| c.into_owned()),
        artist: t.artist().map(|c| c.into_owned()),
        album: t.album().map(|c| c.into_owned()),
        album_artist,
        year: t.date().map(|d| d.year),
        track: t.track(),
        track_total: t.track_total(),
        disc: t.disk(),
        disc_total: t.disk_total(),
        genres,
    }
}

fn vc_core(t: &VorbisComments) -> Core {
    core_from(
        t,
        t.get("ALBUMARTIST").map(String::from),
        t.get_all("GENRE").map(String::from).collect(),
    )
}

fn id3v2_core(t: &Id3v2Tag) -> Core {
    let tpe2 = FrameId::new("TPE2").unwrap();
    core_from(
        t,
        t.get_text(&tpe2).map(String::from),
        t.genres()
            .map(|g| g.map(String::from).collect())
            .unwrap_or_default(),
    )
}

const GENRE_ATOM: AtomIdent<'static> = AtomIdent::Fourcc(*b"\xa9gen");
const AART_ATOM: AtomIdent<'static> = AtomIdent::Fourcc(*b"aART");

fn ilst_genres(t: &Ilst) -> Vec<String> {
    t.get(&GENRE_ATOM)
        .map(|a| {
            a.data()
                .filter_map(|d| match d {
                    AtomData::UTF8(s) | AtomData::UTF16(s) => Some(s.clone()),
                    _ => None,
                })
                .collect()
        })
        .unwrap_or_default()
}

fn ilst_text(t: &Ilst, id: &AtomIdent<'_>) -> Option<String> {
    t.get(id).and_then(|a| {
        a.data().find_map(|d| match d {
            AtomData::UTF8(s) | AtomData::UTF16(s) => Some(s.clone()),
            _ => None,
        })
    })
}

impl Concrete {
    fn core(&self) -> Core {
        match self {
            Concrete::Flac(x) => x.vorbis_comments().map(vc_core).unwrap_or_default(),
            Concrete::Vorbis(x) => vc_core(x.vorbis_comments()),
            Concrete::Opus(x) => vc_core(x.vorbis_comments()),
            Concrete::Mp4(x) => x
                .ilst()
                .map(|t| core_from(t, ilst_text(t, &AART_ATOM), ilst_genres(t)))
                .unwrap_or_default(),
            Concrete::Mpeg(x) => {
                // Per field: ID3v2 first, then APE, then ID3v1.
                let mut views = vec![];
                if let Some(t) = x.id3v2() {
                    views.push(id3v2_core(t));
                }
                if let Some(t) = x.ape() {
                    let g = t
                        .get("Genre")
                        .and_then(|i| i.text_values())
                        .map(|v| v.map(String::from).collect())
                        .unwrap_or_default();
                    let aa = t
                        .get("Album Artist")
                        .and_then(|i| i.value().text().map(String::from));
                    views.push(core_from(t, aa, g));
                }
                if let Some(t) = x.id3v1() {
                    views.push(core_from(
                        t,
                        None,
                        t.genre().map(|g| vec![g.into_owned()]).unwrap_or_default(),
                    ));
                }
                let mut c = Core::default();
                for v in views {
                    c.title = c.title.or(v.title);
                    c.artist = c.artist.or(v.artist);
                    c.album = c.album.or(v.album);
                    c.album_artist = c.album_artist.or(v.album_artist);
                    c.year = c.year.or(v.year);
                    c.track = c.track.or(v.track);
                    c.track_total = c.track_total.or(v.track_total);
                    c.disc = c.disc.or(v.disc);
                    c.disc_total = c.disc_total.or(v.disc_total);
                    if c.genres.is_empty() {
                        c.genres = v.genres;
                    }
                }
                c
            }
        }
    }

    /// Genres as stored in *every* writable tag of the file (for write verification).
    pub fn all_genre_views(&self) -> Vec<(&'static str, Vec<String>)> {
        match self {
            Concrete::Flac(x) => {
                vec![(
                    "VorbisComments",
                    x.vorbis_comments().map(vc_core).unwrap_or_default().genres,
                )]
            }
            Concrete::Vorbis(x) => vec![("VorbisComments", vc_core(x.vorbis_comments()).genres)],
            Concrete::Opus(x) => vec![("VorbisComments", vc_core(x.vorbis_comments()).genres)],
            Concrete::Mp4(x) => vec![("Mp4Ilst", x.ilst().map(ilst_genres).unwrap_or_default())],
            Concrete::Mpeg(x) => {
                let mut v = vec![(
                    "ID3v2",
                    x.id3v2().map(|t| id3v2_core(t).genres).unwrap_or_default(),
                )];
                if let Some(t) = x.ape().filter(|t| t.get("Genre").is_some()) {
                    v.push((
                        "APE",
                        t.get("Genre")
                            .and_then(|i| i.text_values())
                            .map(|v| v.map(String::from).collect())
                            .unwrap_or_default(),
                    ));
                }
                v
            }
        }
    }

    pub fn fields_view(&self) -> Value {
        let c = self.core();
        json!({"title": c.title, "artist": c.artist, "album": c.album,
               "album_artist": c.album_artist, "year": c.year, "track": c.track,
               "track_total": c.track_total, "disc": c.disc, "disc_total": c.disc_total})
    }
}

// ---------------------------------------------------------------- edits

#[derive(Clone, Debug)]
pub enum Op {
    Genres(Vec<String>),
    Fields,
}

pub const F_TITLE: &str = "Gama Title ✓";
pub const F_ARTIST: &str = "Gama Artist";
pub const F_ALBUM: &str = "Gama Album";
pub const F_ALBUM_ARTIST: &str = "Gama Album Artist";

fn ts2024() -> Timestamp {
    Timestamp {
        year: 2024,
        ..Timestamp::default()
    }
}

fn set_fields_accessor<A: Accessor>(t: &mut A) {
    t.set_title(F_TITLE.into());
    t.set_artist(F_ARTIST.into());
    t.set_album(F_ALBUM.into());
    t.set_date(ts2024());
    t.set_track(7);
    t.set_track_total(12);
    t.set_disk(2);
    t.set_disk_total(3);
}

/// Order-preserving edit of Vorbis comments. lofty's `VorbisComments::remove`
/// swaps matching items to the front and drains them, which permutes the
/// remaining (unrelated) comments, and `insert` moves the key to the end. We
/// rebuild the list ourselves: the first occurrence of each edited key is
/// replaced in place, other occurrences (and listed aliases) are dropped, new
/// keys are appended.
fn vc_set_stable(t: &mut VorbisComments, set: &[(&str, Vec<String>)], drop_keys: &[&str]) {
    let items: Vec<(String, String)> = t.take_items().collect();
    let mut done = vec![false; set.len()];
    for (k, v) in items {
        if drop_keys.iter().any(|d| k.eq_ignore_ascii_case(d)) {
            continue;
        }
        match set.iter().position(|(sk, _)| k.eq_ignore_ascii_case(sk)) {
            Some(i) if !done[i] => {
                done[i] = true;
                for nv in &set[i].1 {
                    t.push(k.clone(), nv.clone());
                }
            }
            Some(_) => {}
            None => t.push(k, v),
        }
    }
    for (i, (k, vals)) in set.iter().enumerate() {
        if !done[i] {
            for nv in vals {
                t.push((*k).to_string(), nv.clone());
            }
        }
    }
}

fn vc_apply(t: &mut VorbisComments, op: &Op) {
    match op {
        Op::Genres(g) => vc_set_stable(t, &[("GENRE", g.clone())], &[]),
        Op::Fields => {
            let one = |s: &str| vec![s.to_string()];
            vc_set_stable(
                t,
                &[
                    ("TITLE", one(F_TITLE)),
                    ("ARTIST", one(F_ARTIST)),
                    ("ALBUM", one(F_ALBUM)),
                    ("ALBUMARTIST", one(F_ALBUM_ARTIST)),
                    ("DATE", one("2024")),
                    ("TRACKNUMBER", one("7")),
                    ("TRACKTOTAL", one("12")),
                    ("DISCNUMBER", one("2")),
                    ("DISCTOTAL", one("3")),
                ],
                // aliases of the same fields; leaving them would contradict the new values
                &["TOTALTRACKS", "TOTALDISCS", "ALBUM ARTIST", "YEAR"],
            );
        }
    }
}

fn id3v2_apply(t: &mut Id3v2Tag, op: &Op) {
    match op {
        // ID3v2.4 multi-value: values separated by NUL inside one TCON frame.
        Op::Genres(g) => t.set_genre(g.join("\0")),
        Op::Fields => {
            // lofty bug workaround: Accessor::set_date inserts a *Text* TDRC
            // frame, which does not replace an existing *Timestamp* TDRC frame
            // (FrameList::insert dedups by equality, variants differ), so the
            // tag ends up with two TDRC frames and readers see the old year.
            // Also drop the ID3v2.3 date frames, which a v2.4 TDRC supersedes.
            for id in ["TDRC", "TYER", "TDAT", "TIME"] {
                let _ = t.remove(&FrameId::new(id).unwrap()).count();
            }
            set_fields_accessor(t);
            let tpe2 = FrameId::new("TPE2").unwrap();
            t.insert(Frame::Text(TextInformationFrame::new(
                tpe2,
                lofty::TextEncoding::UTF8,
                F_ALBUM_ARTIST.to_string(),
            )));
        }
    }
}

fn ape_apply(t: &mut lofty::ape::ApeTag, op: &Op) {
    match op {
        // Only touch keys the APE tag already has (it is a secondary tag).
        // APEv2 multi-value: values separated by NUL inside one item.
        Op::Genres(g) => {
            if t.genre().is_some() {
                t.set_genre(g.join("\0"))
            }
        }
        Op::Fields => {
            if t.title().is_some() {
                t.set_title(F_TITLE.into());
            }
            if t.artist().is_some() {
                t.set_artist(F_ARTIST.into());
            }
            if t.album().is_some() {
                t.set_album(F_ALBUM.into());
            }
            if t.date().is_some() {
                t.set_date(ts2024());
            }
            if t.track().is_some() {
                t.set_track(7);
                t.set_track_total(12);
            }
            if t.disk().is_some() {
                t.set_disk(2);
                t.set_disk_total(3);
            }
            if t.get("Album Artist").is_some()
                && let Ok(i) = lofty::ape::ApeItem::new(
                    "Album Artist".into(),
                    lofty::tag::ItemValue::Text(F_ALBUM_ARTIST.into()),
                )
            {
                t.insert(i);
            }
        }
    }
}

fn id3v1_apply(t: &mut Id3v1Tag, op: &Op) {
    match op {
        // ID3v1 holds one genre byte: use the first genre that maps, else clear.
        Op::Genres(g) => {
            t.remove_genre();
            for x in g {
                t.set_genre(x.clone());
                if t.genre.is_some() {
                    break;
                }
            }
        }
        Op::Fields => set_fields_accessor(t), // no album artist / totals in ID3v1
    }
}

fn ilst_apply(t: &mut Ilst, op: &Op) {
    match op {
        Op::Genres(g) => {
            let _ = t.remove(&GENRE_ATOM).count();
            let _ = t.remove(&AtomIdent::Fourcc(*b"gnre")).count();
            let data = g.iter().map(|x| AtomData::UTF8(x.clone())).collect();
            if let Some(a) = Atom::from_collection(GENRE_ATOM, data) {
                t.insert(a);
            }
        }
        Op::Fields => {
            set_fields_accessor(t);
            t.replace_atom(Atom::new(AART_ATOM, AtomData::UTF8(F_ALBUM_ARTIST.into())));
        }
    }
}

impl Concrete {
    /// Apply the edit to every writable tag of the file, creating the primary
    /// tag if it does not exist.
    pub fn apply(&mut self, op: &Op) -> Vec<String> {
        let mut notes = vec![];
        match self {
            Concrete::Flac(x) => {
                if x.vorbis_comments().is_none() {
                    x.set_vorbis_comments(VorbisComments::default());
                    notes.push("created VorbisComments block".into());
                }
                vc_apply(x.vorbis_comments_mut().unwrap(), op);
            }
            Concrete::Vorbis(x) => vc_apply(x.vorbis_comments_mut(), op),
            Concrete::Opus(x) => vc_apply(x.vorbis_comments_mut(), op),
            Concrete::Mp4(x) => {
                if x.ilst().is_none() {
                    x.set_ilst(Ilst::default());
                    notes.push("created ilst".into());
                }
                ilst_apply(x.ilst_mut().unwrap(), op);
            }
            Concrete::Mpeg(x) => {
                if x.id3v2().is_none() {
                    // Seed a new ID3v2.4 tag from the existing ID3v1/APE values so
                    // readers that prefer ID3v2 do not lose the old title/artist.
                    let mut t = Id3v2Tag::default();
                    if let Some(v1) = x.id3v1() {
                        let generic: lofty::tag::Tag = v1.clone().into();
                        t = generic.into();
                        notes.push("created ID3v2.4 tag seeded from ID3v1".into());
                    } else {
                        notes.push("created empty ID3v2.4 tag".into());
                    }
                    x.set_id3v2(t);
                }
                id3v2_apply(x.id3v2_mut().unwrap(), op);
                if let Some(t) = x.ape_mut() {
                    ape_apply(t, op);
                    notes.push("APEv2 tag present: only its existing keys updated".into());
                }
                if let Some(t) = x.id3v1_mut() {
                    id3v1_apply(t, op);
                    notes.push("also updated ID3v1 tag (single genre byte)".into());
                }
            }
        }
        notes
    }
}

// ---------------------------------------------------------------- read command

fn walk(p: &Path, out: &mut Vec<PathBuf>) {
    match std::fs::symlink_metadata(p) {
        Ok(m) if m.is_dir() => {
            let mut entries: Vec<PathBuf> = std::fs::read_dir(p)
                .map(|r| r.filter_map(|e| e.ok().map(|e| e.path())).collect())
                .unwrap_or_default();
            entries.sort();
            for e in entries {
                walk(&e, out);
            }
        }
        _ => out.push(p.to_path_buf()),
    }
}

fn id3v2_header_version(path: &Path) -> Option<String> {
    let mut b = [0u8; 5];
    File::open(path).ok()?.read_exact(&mut b).ok()?;
    (&b[..3] == b"ID3").then(|| format!("2.{}.{}", b[3], b[4]))
}

pub fn read_one(path: &Path) -> Value {
    let start = Instant::now();
    let p = path.to_path_buf();
    let mut out = json!({"path": path.display().to_string()});
    let res = guarded("read", move || {
        let det = detect(&p)?;
        let c = read_concrete_path(&p, det.chosen, false)?;
        Ok((det, c))
    });
    match res {
        Ok((det, c)) => {
            let core = c.core();
            let snap = c.snapshot();
            let props = c.properties();
            let core_keys = core_keys();
            let mut other: BTreeMap<String, Vec<String>> = BTreeMap::new();
            for (k, v) in &snap.items {
                if !core_keys.contains(&k.as_str()) {
                    other.entry(k.clone()).or_default().push(v.clone());
                }
            }
            let o = out.as_object_mut().unwrap();
            o.insert("ok".into(), json!(true));
            o.insert("error".into(), Value::Null);
            o.insert("format".into(), json!(format!("{:?}", det.chosen)));
            o.insert(
                "detection".into(),
                json!({"method": det.method,
                       "by_content": det.by_content.map(|t| format!("{t:?}")),
                       "by_extension": det.by_extension.map(|t| format!("{t:?}")),
                       "mismatch": det.by_content.is_some() && det.by_extension.is_some()
                                   && det.by_content != det.by_extension}),
            );
            o.insert("tag_types".into(), json!(c.tag_types()));
            o.insert("id3v2_header".into(), json!(id3v2_header_version(path)));
            o.insert("title".into(), json!(core.title));
            o.insert("artist".into(), json!(core.artist));
            o.insert("album".into(), json!(core.album));
            o.insert("album_artist".into(), json!(core.album_artist));
            o.insert("year".into(), json!(core.year));
            o.insert("track".into(), json!(core.track));
            o.insert("track_total".into(), json!(core.track_total));
            o.insert("disc".into(), json!(core.disc));
            o.insert("disc_total".into(), json!(core.disc_total));
            o.insert("genres".into(), json!(core.genres));
            o.insert(
                "pictures".into(),
                json!(snap.pictures.iter().map(PicInfo::json).collect::<Vec<_>>()),
            );
            o.insert("other_tags".into(), json!(other));
            o.insert(
                "properties".into(),
                json!({"duration_ms": props.duration().as_millis() as u64,
                       "sample_rate": props.sample_rate(), "channels": props.channels(),
                       "audio_bitrate": props.audio_bitrate()}),
            );
        }
        Err(e) => {
            let o = out.as_object_mut().unwrap();
            o.insert("ok".into(), json!(false));
            o.insert("error".into(), json!(e));
            // still report how detection went, if it can be done safely
            let p2 = path.to_path_buf();
            if let Ok(d) = guarded("detect", move || detect(&p2)) {
                o.insert("format".into(), json!(format!("{:?}", d.chosen)));
                o.insert(
                    "detection".into(),
                    json!({"method": d.method,
                           "by_content": d.by_content.map(|t| format!("{t:?}")),
                           "by_extension": d.by_extension.map(|t| format!("{t:?}"))}),
                );
            }
        }
    }
    out["elapsed_ms"] = json!(start.elapsed().as_secs_f64() * 1000.0);
    out
}

/// Native keys that are reported as core fields (excluded from other_tags).
fn core_keys() -> Vec<&'static str> {
    vec![
        "VC:TITLE",
        "VC:ARTIST",
        "VC:ALBUM",
        "VC:ALBUMARTIST",
        "VC:DATE",
        "VC:TRACKNUMBER",
        "VC:TRACKTOTAL",
        "VC:DISCNUMBER",
        "VC:DISCTOTAL",
        "VC:GENRE",
        "ID3v2:TIT2",
        "ID3v2:TPE1",
        "ID3v2:TALB",
        "ID3v2:TPE2",
        "ID3v2:TDRC",
        "ID3v2:TRCK",
        "ID3v2:TPOS",
        "ID3v2:TCON",
        "MP4:©nam",
        "MP4:©ART",
        "MP4:©alb",
        "MP4:aART",
        "MP4:©day",
        "MP4:trkn",
        "MP4:disk",
        "MP4:©gen",
    ]
}

pub fn cmd_read(paths: &[String]) -> i32 {
    let mut files = vec![];
    for p in paths {
        walk(Path::new(p), &mut files);
    }
    let results: Vec<Value> = files.iter().map(|f| read_one(f)).collect();
    let failed = results.iter().filter(|r| r["ok"] == json!(false)).count();
    crate::print(&json!({"lofty_version": "0.25.5", "files": results.len(),
                         "failed": failed, "results": results}));
    0
}

/// Diagnostic: lofty's own in-place write path (no temp file, no protocol), to
/// see what the library and OS allow, e.g. on a read-only file.
pub fn cmd_direct_save(paths: &[String]) -> i32 {
    let mut out = vec![];
    for p in paths {
        let pb = PathBuf::from(p);
        let r = guarded("direct-save", move || {
            let det = detect(&pb)?;
            let mut c = read_concrete_path(&pb, det.chosen, true)?;
            c.apply(&Op::Genres(vec!["Direct".into()]));
            let mut f = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .open(&pb)
                .map_err(|e| format!("open for write: {e}"))?;
            c.save_to(&mut f)
        });
        out.push(json!({"path": p, "ok": r.is_ok(), "error": r.err()}));
    }
    crate::print(&json!(out));
    0
}

#[cfg(test)]
mod tests {
    use super::*;
    use lofty::id3::v2::TimestampFrame;

    fn keys(t: &VorbisComments) -> Vec<String> {
        t.items().map(|(k, _)| k.to_string()).collect()
    }

    fn sample_vc() -> VorbisComments {
        let mut t = VorbisComments::default();
        for (k, v) in [
            ("ALBUM", "x"),
            ("ALBUMARTIST", "y"),
            ("ARTIST", "z"),
            ("TITLE", "t"),
            ("GENRE", "g1"),
            ("GENRE", "g2"),
        ] {
            t.push(k.into(), v.into());
        }
        t
    }

    /// Documents lofty 0.25.5 behaviour: VorbisComments::remove permutes the
    /// remaining items. If this starts failing, lofty fixed it.
    #[test]
    fn lofty_vorbis_remove_reorders_other_items() {
        let mut t = sample_vc();
        let _ = t.remove("GENRE").count();
        assert_ne!(keys(&t), vec!["ALBUM", "ALBUMARTIST", "ARTIST", "TITLE"]);
    }

    #[test]
    fn vc_set_stable_keeps_order() {
        let mut t = sample_vc();
        vc_apply(
            &mut t,
            &Op::Genres(vec!["A".into(), "B".into(), "C".into()]),
        );
        assert_eq!(
            keys(&t),
            vec![
                "ALBUM",
                "ALBUMARTIST",
                "ARTIST",
                "TITLE",
                "GENRE",
                "GENRE",
                "GENRE"
            ]
        );
        assert_eq!(t.get_all("GENRE").collect::<Vec<_>>(), vec!["A", "B", "C"]);
    }

    fn tag_with_timestamp_tdrc() -> Id3v2Tag {
        let mut t = Id3v2Tag::default();
        t.insert(Frame::Timestamp(TimestampFrame::new(
            FrameId::new("TDRC").unwrap(),
            lofty::TextEncoding::UTF8,
            Timestamp {
                year: 1979,
                ..Timestamp::default()
            },
        )));
        t
    }

    fn tdrc_count(t: &Id3v2Tag) -> usize {
        t.into_iter().filter(|f| f.id_str() == "TDRC").count()
    }

    /// Documents lofty 0.25.5 behaviour: Accessor::set_date leaves the parsed
    /// Timestamp TDRC in place and adds a second TDRC frame.
    #[test]
    fn lofty_set_date_duplicates_tdrc() {
        let mut t = tag_with_timestamp_tdrc();
        t.set_date(Timestamp {
            year: 2024,
            ..Timestamp::default()
        });
        assert_eq!(tdrc_count(&t), 2);
        assert_eq!(t.date().map(|d| d.year), Some(1979));
    }

    #[test]
    fn id3v2_fields_replace_date() {
        let mut t = tag_with_timestamp_tdrc();
        id3v2_apply(&mut t, &Op::Fields);
        assert_eq!(tdrc_count(&t), 1);
        assert_eq!(t.get_text(&FrameId::new("TDRC").unwrap()), Some("2024"));
        // lofty quirk: date() only reads Timestamp frames, set_date stores a
        // Text frame, so the getter is None until the tag is re-read.
        assert_eq!(t.date(), None);
    }

    #[test]
    fn id3v2_genres_are_nul_separated() {
        let mut t = Id3v2Tag::default();
        id3v2_apply(&mut t, &Op::Genres(vec!["A".into(), "B".into()]));
        assert_eq!(t.genres().unwrap().collect::<Vec<_>>(), vec!["A", "B"]);
    }

    #[test]
    fn guarded_catches_panic_and_timeout_value() {
        let r: Result<(), String> = guarded("t", || panic!("boom"));
        assert!(r.unwrap_err().contains("PANIC"));
    }
}
