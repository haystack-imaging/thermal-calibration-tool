//! Capture time of an image: XMP, then EXIF / TIFF tags, then the file date.
//!
//! Times are kept as the camera's local wall time. A zone suffix is recorded but never applied: EXIF
//! times are naive local wall time and XMP times from the same camera carry the local offset, so
//! stripping the offset keeps both consistent. A trailing 'Z' is kept as a flag so the pre-flight
//! check can warn about mixed zones.

use crate::model::Timestamp;
use chrono::NaiveDateTime;
use regex::bytes::Regex as BytesRegex;
use regex::Regex;
use std::fs::File;
use std::io::{BufReader, Read};
use std::path::Path;
use std::sync::LazyLock;

static TZ_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(Z|[+-]\d{2}(?::?\d{2})?)$").expect("static regex"));
static XMP_RES: LazyLock<Vec<BytesRegex>> = LazyLock::new(|| {
    [
        r#"xmp:CreateDate="([^"]+)""#,
        r#"CreateDate="([^"]+)""#,
        r"<xmp:CreateDate>([^<]+)</xmp:CreateDate>",
    ]
    .iter()
    .map(|p| BytesRegex::new(p).expect("static regex"))
    .collect()
});

/// Parse a date string into (naive datetime, zone suffix). Mirrors the Python `_parse_datetime_string`:
/// ISO 8601 and EXIF (`2026:06:01 10:00:00`) forms, with or without fractional seconds; a date without
/// a time is useless for interpolation and gives None.
pub fn parse_datetime_string(input: &str) -> (Option<NaiveDateTime>, String) {
    let trimmed = input.trim();
    if trimmed.is_empty() || !trimmed.is_ascii() {
        return (None, String::new());
    }
    let mut s = trimmed.to_string();
    let mut tz = String::new();
    if s.len() > 10 {
        if let Some(m) = TZ_RE.find(&s) {
            tz = m.as_str().to_string();
            s.truncate(m.start());
        }
    }
    if s.len() < 16 || !matches!(&s[10..11], "T" | " ") {
        return (None, String::new());
    }
    let mut candidates = vec![s.clone()];
    if &s[4..5] == ":" && &s[7..8] == ":" {
        candidates.push(format!("{}-{}-{}", &s[..4], &s[5..7], &s[8..]));
    }
    for cand in &candidates {
        let iso = cand.replacen(' ', "T", 1);
        for fmt in [
            "%Y-%m-%dT%H:%M:%S%.f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M",
        ] {
            if let Ok(dt) = NaiveDateTime::parse_from_str(&iso, fmt) {
                return (Some(dt), tz);
            }
        }
    }
    for fmt in [
        "%Y:%m:%d %H:%M:%S",
        "%Y:%m:%d %H:%M:%S%.f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S%.f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%.f",
    ] {
        if let Ok(dt) = NaiveDateTime::parse_from_str(&s, fmt) {
            return (Some(dt), tz);
        }
    }
    (None, String::new())
}

fn find(hay: &[u8], needle: &[u8]) -> Option<usize> {
    hay.windows(needle.len()).position(|w| w == needle)
}

fn xmp_timestamp(path: &Path) -> Option<Timestamp> {
    let mut f = File::open(path).ok()?;
    let mut head = Vec::new();
    f.by_ref()
        .take(4 * 1024 * 1024)
        .read_to_end(&mut head)
        .ok()?; // metadata sits at the start
    let xmp: &[u8] = match (
        find(&head, b"<?xpacket begin"),
        find(&head, b"<?xpacket end"),
    ) {
        (Some(a), Some(b)) if a < b => &head[a..(b + 200).min(head.len())],
        _ => &head[..head.len().min(262_144)],
    };
    for re in XMP_RES.iter() {
        if let Some(c) = re.captures(xmp) {
            let text = String::from_utf8_lossy(&c[1]);
            let (dt, tz) = parse_datetime_string(&text);
            if let Some(dt) = dt {
                return Some(Timestamp::new(dt, "XMP", &tz));
            }
        }
    }
    None
}

fn ascii_value(v: &exif::Value) -> Option<String> {
    match v {
        exif::Value::Ascii(vecs) => vecs.first().map(|b| {
            String::from_utf8_lossy(b)
                .trim_end_matches('\0')
                .to_string()
        }),
        _ => None,
    }
}

fn exif_timestamp(path: &Path) -> Option<Timestamp> {
    let f = File::open(path).ok()?;
    let mut br = BufReader::new(f);
    let ex = exif::Reader::new().read_from_container(&mut br).ok()?;
    for (tag, source) in [
        (exif::Tag::DateTimeOriginal, "EXIF"),
        (exif::Tag::DateTime, "TIFF tag"),
    ] {
        if let Some(field) = ex.get_field(tag, exif::In::PRIMARY) {
            if let Some(text) = ascii_value(&field.value) {
                let (dt, tz) = parse_datetime_string(&text);
                if let Some(dt) = dt {
                    return Some(Timestamp::new(dt, source, &tz));
                }
            }
        }
    }
    None
}

/// Capture time of an image. Tries the XMP packet (DJI writes `xmp:CreateDate` with the local
/// offset), then EXIF `DateTimeOriginal`, then the plain TIFF `DateTime` tag, then the file
/// modification time (which changes on every copy, so the source is recorded for the log).
pub fn extract_timestamp(path: &Path) -> Timestamp {
    if let Some(t) = xmp_timestamp(path) {
        return t;
    }
    if let Some(t) = exif_timestamp(path) {
        return t;
    }
    match std::fs::metadata(path).and_then(|m| m.modified()) {
        Ok(t) => {
            let local: chrono::DateTime<chrono::Local> = t.into();
            Timestamp::new(local.naive_local(), "file mtime", "")
        }
        Err(_) => Timestamp::none(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dt(s: &str) -> NaiveDateTime {
        NaiveDateTime::parse_from_str(s, "%Y-%m-%d %H:%M:%S").unwrap()
    }

    #[test]
    fn parses_the_forms_the_python_tool_accepts() {
        assert_eq!(
            parse_datetime_string("2026:06:01 10:00:00"),
            (Some(dt("2026-06-01 10:00:00")), String::new())
        );
        assert_eq!(
            parse_datetime_string("2026-06-01T10:00:00+02:00"),
            (Some(dt("2026-06-01 10:00:00")), "+02:00".into())
        );
        assert_eq!(
            parse_datetime_string("2026-06-01T10:00:00Z"),
            (Some(dt("2026-06-01 10:00:00")), "Z".into())
        );
        assert_eq!(
            parse_datetime_string("2026-06-01 10:00:00.250"),
            (
                Some(dt("2026-06-01 10:00:00") + chrono::Duration::milliseconds(250)),
                String::new()
            )
        );
        assert_eq!(
            parse_datetime_string("2026-06-01T10:00+0200").0,
            Some(dt("2026-06-01 10:00:00"))
        );
        assert_eq!(
            parse_datetime_string("2026-06-01").0,
            None,
            "a date without a time is useless"
        );
        assert_eq!(parse_datetime_string("").0, None);
        assert_eq!(parse_datetime_string("yesterday").0, None);
    }
}
