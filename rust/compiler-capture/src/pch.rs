//! Resolve saved PCH source buffers from already committed producer records.
use crate::storage::{Artifact, Invocation};
use serde::{Deserialize, Serialize};
use std::{
    ffi::OsStr,
    fs, io,
    os::unix::{ffi::OsStrExt, fs::MetadataExt},
    path::Path,
};

#[derive(Serialize, Deserialize)]
pub struct InputState {
    pub path_hex: String,
    size: u64,
    mtime: i64,
    nanos: i64,
}

fn decode(value: &str) -> io::Result<Vec<u8>> {
    if !value.len().is_multiple_of(2) {
        return Err(io::Error::other("invalid PCH path bytes"));
    }
    value
        .as_bytes()
        .as_chunks::<2>()
        .0
        .iter()
        .map(|pair| {
            u8::from_str_radix(std::str::from_utf8(pair).map_err(io::Error::other)?, 16)
                .map_err(io::Error::other)
        })
        .collect()
}

pub fn state(path: &Path) -> io::Result<Option<InputState>> {
    let metadata = match fs::metadata(path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    if !metadata.is_file() {
        return Ok(None);
    }
    Ok(Some(InputState {
        path_hex: crate::storage::hex(path.as_os_str().as_bytes()),
        size: metadata.len(),
        mtime: metadata.mtime(),
        nanos: metadata.mtime_nsec(),
    }))
}

pub fn remember(
    invocation: &mut Invocation,
    pch: &Artifact,
    states: &[InputState],
) -> io::Result<()> {
    if states.len() > 4096 {
        return Err(io::Error::other("too many PCH input dependencies"));
    }
    // Timestamps are invocation-control state only. They never enter the
    // canonical records or the published output.
    invocation.pch_control(&pch.sha256, &serde_json::to_vec(states)?)
}

pub fn sources(invocation: &Invocation, pch: &Artifact) -> io::Result<Vec<Artifact>> {
    let control = invocation
        .root
        .join("pch-inputs")
        .join(format!("{}.json", pch.sha256));
    if control.exists() {
        if fs::metadata(&control)?.len() > 4 * 1024 * 1024 {
            return Err(io::Error::other("PCH input index exceeds its limit"));
        }
        let states: Vec<InputState> = serde_json::from_reader(fs::File::open(control)?)?;
        for state in states {
            let path = decode(&state.path_hex)?;
            let current = fs::metadata(Path::new(OsStr::from_bytes(&path)));
            if !current.is_ok_and(|metadata| {
                metadata.len() == state.size
                    && metadata.mtime() == state.mtime
                    && metadata.mtime_nsec() == state.nanos
            }) {
                return Ok(Vec::new());
            }
        }
    }
    let mut scanned = 0;
    for entry in fs::read_dir(invocation.root.join("records"))?.take(4097) {
        let path = entry?.path();
        if path.extension().is_none_or(|extension| extension != "json") {
            continue;
        }
        let length = fs::metadata(&path)?.len();
        scanned += length;
        if length > 4 * 1024 * 1024 || scanned > 16 * 1024 * 1024 {
            return Err(io::Error::other(
                "PCH producer index exceeds its metadata limit",
            ));
        }
        let record: serde_json::Value = serde_json::from_reader(fs::File::open(path)?)?;
        if record["analysis"]["pch"] != true {
            continue;
        }
        let artifacts: Vec<Artifact> = serde_json::from_value(record["artifacts"].clone())?;
        if artifacts
            .iter()
            .any(|artifact| artifact.role == "pch" && artifact.sha256 == pch.sha256)
        {
            let sources = artifacts
                .into_iter()
                .filter(|artifact| {
                    artifact.role == "preprocessing" && artifact.path_base == "invocation-stage"
                })
                .collect::<Vec<_>>();
            for source in &sources {
                crate::storage::verify_object(
                    &invocation.root.join("objects").join(&source.sha256),
                    &source.sha256,
                    source.size,
                )?;
            }
            return Ok(sources);
        }
    }
    Ok(Vec::new())
}

pub fn name(artifact: &Artifact) -> io::Result<Vec<u8>> {
    let bytes = artifact.original_path_hex.as_bytes();
    if !bytes.len().is_multiple_of(2) {
        return Err(io::Error::other("invalid saved PCH source name"));
    }
    bytes
        .as_chunks::<2>()
        .0
        .iter()
        .map(|pair| {
            let text = std::str::from_utf8(pair).map_err(io::Error::other)?;
            u8::from_str_radix(text, 16).map_err(io::Error::other)
        })
        .collect()
}
