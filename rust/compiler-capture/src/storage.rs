use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::fs::{MetadataExt, OpenOptionsExt},
    },
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
};

const FILE_LIMIT: u64 = 16 * 1024 * 1024;
const INVOCATION_LIMIT: u64 = 48 * 1024 * 1024;
const MAX_FILE_LIMIT: u64 = 128 * 1024 * 1024;
const RECORD_LIMIT: u64 = 4 * 1024 * 1024;
const MANIFEST_LIMIT: usize = 16 * 1024 * 1024;
const FILE_COUNT_LIMIT: u64 = 4096;
static SERIAL: AtomicU64 = AtomicU64::new(0);
pub const AUXILIARY_FD: i32 = 198;
pub const AUXILIARY_VIEW: &str = "/proc/self/fd/198";

pub fn descriptor_available() -> bool {
    unsafe {
        libc::fcntl(AUXILIARY_FD, libc::F_GETFD) == -1 && *libc::__errno_location() == libc::EBADF
    }
}

pub struct DirectoryView {
    _directory: File,
    pub path: PathBuf,
}

impl DirectoryView {
    pub fn acquire(invocation: &Invocation) -> io::Result<Self> {
        if !descriptor_available() {
            return Err(io::Error::other("capture directory descriptor is occupied"));
        }
        let source = File::open(&invocation.aux)?;
        let directory = if source.as_raw_fd() == AUXILIARY_FD {
            if unsafe { libc::fcntl(AUXILIARY_FD, libc::F_SETFD, 0) } < 0 {
                return Err(io::Error::last_os_error());
            }
            source
        } else {
            if unsafe { libc::dup3(source.as_raw_fd(), AUXILIARY_FD, 0) } < 0 {
                return Err(io::Error::last_os_error());
            }
            unsafe { File::from_raw_fd(AUXILIARY_FD) }
        };
        let path = std::env::var_os("DATASET_CAPTURE_VIEW")
            .map(PathBuf::from)
            .unwrap_or_else(|| invocation.root.join("descriptor-view"));
        if !path.is_absolute() {
            return Err(io::Error::other("capture view must be absolute"));
        }
        if fs::read_link(&path)? != Path::new(AUXILIARY_VIEW) {
            return Err(io::Error::other("invalid capture descriptor view"));
        }
        Ok(Self {
            _directory: directory,
            path,
        })
    }
}

pub fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
pub fn hash(bytes: &[u8]) -> String {
    hex(&Sha256::digest(bytes))
}

pub fn bounded_generation_filesystem(path: &Path) -> bool {
    use std::os::unix::ffi::OsStrExt;
    let Ok(path) = std::ffi::CString::new(path.as_os_str().as_bytes()) else {
        return false;
    };
    let mut stats: libc::statfs = unsafe { std::mem::zeroed() };
    if unsafe { libc::statfs(path.as_ptr(), &mut stats) } != 0 {
        return false;
    }
    // The measured harness uses tmpfs. Do not infer an upper-layer quota from
    // an arbitrary overlay mount, or a process limit from a daemon client.
    stats.f_type == libc::TMPFS_MAGIC
        && stats
            .f_blocks
            .checked_mul(stats.f_bsize as u64)
            .is_some_and(|bytes| bytes > 0 && bytes <= 4 * 1024 * 1024 * 1024)
        && stats.f_files > 0
        && stats.f_files <= 131072
}

#[derive(Serialize, Deserialize, Clone, Debug)]
#[serde(deny_unknown_fields)]
pub struct Artifact {
    pub sha256: String,
    pub size: u64,
    pub role: String,
    pub path_base: String,
    pub original_path_hex: String,
}

#[derive(Serialize, Deserialize, Clone, Copy, Default)]
struct Account {
    bytes: u64,
    files: u64,
}

pub struct Invocation {
    pub root: PathBuf,
    pub directory: PathBuf,
    pub aux: PathBuf,
    pub id: String,
    used: u64,
    limit: u64,
    artifact_limit: u64,
    invocation_limit: u64,
}

pub struct Stream {
    path: PathBuf,
    file: File,
    size: u64,
}

struct Lock(File);
impl Lock {
    fn acquire(root: &Path) -> io::Result<Self> {
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .mode(0o600)
            .open(root.join(".lock"))?;
        loop {
            if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) } == 0 {
                break;
            }
            let error = io::Error::last_os_error();
            if error.kind() != io::ErrorKind::Interrupted {
                return Err(error);
            }
        }
        Ok(Self(file))
    }
}
impl Drop for Lock {
    fn drop(&mut self) {
        unsafe {
            libc::flock(self.0.as_raw_fd(), libc::LOCK_UN);
        }
    }
}

fn atomic_write(path: &Path, bytes: &[u8]) -> io::Result<()> {
    let tmp = path.with_extension(format!(
        "tmp-{}-{}",
        std::process::id(),
        SERIAL.fetch_add(1, Ordering::Relaxed)
    ));
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&tmp)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    fs::rename(&tmp, path)?;
    File::open(path.parent().unwrap())?.sync_all()
}

impl Invocation {
    pub fn start_stream(&mut self) -> io::Result<Stream> {
        self.reserve(4096, 1)?;
        let path = self.directory.join("stdout.part");
        let file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&path)?;
        Ok(Stream {
            path,
            file,
            size: 0,
        })
    }

    pub fn stream_chunk(&mut self, stream: &mut Stream, data: &[u8]) -> io::Result<()> {
        if stream.size + data.len() as u64 > self.artifact_limit {
            return Err(io::Error::other("stdout artifact exceeds its byte limit"));
        }
        self.reserve(data.len() as u64, 0)?;
        stream.file.write_all(data)?;
        stream.size += data.len() as u64;
        Ok(())
    }

    pub fn finish_stream(&mut self, stream: Stream) -> io::Result<Artifact> {
        stream.file.sync_all()?;
        drop(stream.file);
        let result = self.snapshot(&stream.path, "preprocessing", "stdout", b"stdout");
        fs::remove_file(stream.path)?;
        self.release(stream.size + 4096, 1)?;
        result
    }

    pub fn discard_stream(&mut self, stream: Stream) -> io::Result<()> {
        drop(stream.file);
        fs::remove_file(stream.path)?;
        self.release(stream.size + 4096, 1)
    }

    pub fn start(root: &Path) -> io::Result<Self> {
        {
            let _guard = Lock::acquire(root)?;
            let (attempts, completed) = counters(root)?;
            set_counters(
                root,
                attempts
                    .checked_add(1)
                    .ok_or_else(|| io::Error::other("invocation count overflow"))?,
                completed,
            )?;
        }
        let limit = std::env::var("DATASET_CAPTURE_LIMIT")
            .unwrap_or_else(|_| "67108864".into())
            .parse::<u64>()
            .map_err(|_| io::Error::other("invalid capture limit"))?;
        if limit == 0 || limit > 1024 * 1024 * 1024 {
            return Err(io::Error::other(
                "capture limit must be between 1 and 1024 MiB",
            ));
        }
        let artifact_limit = std::env::var("DATASET_CAPTURE_ARTIFACT_LIMIT")
            .ok()
            .map(|value| value.parse::<u64>())
            .transpose()
            .map_err(|_| io::Error::other("invalid artifact limit"))?
            .unwrap_or(FILE_LIMIT);
        let invocation_limit = std::env::var("DATASET_CAPTURE_INVOCATION_LIMIT")
            .ok()
            .map(|value| value.parse::<u64>())
            .transpose()
            .map_err(|_| io::Error::other("invalid invocation limit"))?
            .unwrap_or(INVOCATION_LIMIT);
        if artifact_limit == 0
            || artifact_limit > MAX_FILE_LIMIT
            || invocation_limit < artifact_limit
            || invocation_limit > 512 * 1024 * 1024
        {
            return Err(io::Error::other(
                "invalid capture artifact/invocation limits",
            ));
        }
        let mut random = [0u8; 16];
        File::open("/dev/urandom")?.read_exact(&mut random)?;
        let id = hex(&random);
        let directory = root.join("pending").join(&id);
        fs::create_dir(&directory)?;
        let aux = directory.join("aux");
        fs::create_dir(&aux)?;
        let mut invocation = Self {
            root: root.into(),
            directory,
            aux,
            id,
            used: 0,
            limit,
            artifact_limit,
            invocation_limit,
        };
        // Reserve directory blocks, bounded record/manifest metadata and the
        // accounting update before allocating artifacts. Crashed reservations
        // remain charged. Conservative overcounting is preferable to overshoot.
        invocation.reserve(128 * 1024, 4)?;
        Ok(invocation)
    }

    fn reserve(&mut self, bytes: u64, files: u64) -> io::Result<()> {
        let _guard = Lock::acquire(&self.root)?;
        let state = self.root.join(".usage");
        let mut account: Account = if state.exists() {
            let file = File::open(&state)?;
            if file.metadata()?.len() > 1024 {
                return Err(io::Error::other("invalid capture accounting"));
            }
            serde_json::from_reader(file)?
        } else {
            Account::default()
        };
        let bytes = bytes
            .checked_add(4095)
            .ok_or_else(|| io::Error::other("size overflow"))?
            / 4096
            * 4096;
        if bytes > self.limit.saturating_sub(account.bytes)
            || bytes > self.invocation_limit.saturating_sub(self.used)
            || files > FILE_COUNT_LIMIT.saturating_sub(account.files)
        {
            return Err(io::Error::other("capture reservation limit exceeded"));
        }
        account.bytes += bytes;
        account.files += files;
        atomic_write(&state, &serde_json::to_vec(&account)?)?;
        self.used += bytes;
        Ok(())
    }

    fn release(&mut self, bytes: u64, files: u64) -> io::Result<()> {
        let bytes = bytes
            .checked_add(4095)
            .ok_or_else(|| io::Error::other("size overflow"))?
            / 4096
            * 4096;
        let _guard = Lock::acquire(&self.root)?;
        let state = self.root.join(".usage");
        let mut account: Account = serde_json::from_reader(File::open(&state)?)?;
        account.bytes = account
            .bytes
            .checked_sub(bytes)
            .ok_or_else(|| io::Error::other("capture accounting underflow"))?;
        account.files = account
            .files
            .checked_sub(files)
            .ok_or_else(|| io::Error::other("capture file accounting underflow"))?;
        atomic_write(&state, &serde_json::to_vec(&account)?)?;
        self.used = self
            .used
            .checked_sub(bytes)
            .ok_or_else(|| io::Error::other("invocation accounting underflow"))?;
        Ok(())
    }

    pub fn snapshot(
        &mut self,
        path: &Path,
        role: &str,
        base: &str,
        original: &[u8],
    ) -> io::Result<Artifact> {
        let mut source = OpenOptions::new()
            .read(true)
            .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
            .open(path)?;
        let before = source.metadata()?;
        if !before.is_file() {
            return Err(io::Error::other("artifact is not a regular file"));
        }
        if before.len() > self.artifact_limit {
            return Err(io::Error::other(format!(
                "artifact has {} bytes, exceeding its {} byte limit",
                before.len(),
                self.artifact_limit
            )));
        }
        // A primary output is often already captured at its producer's exit.
        // Hash it before reserving another temporary copy. Reusing an existing
        // immutable CAS object needs no new disk allocation or quota refund.
        let mut digest = Sha256::new();
        let mut buffer = [0u8; 8192];
        let mut scanned = 0u64;
        loop {
            crate::process::check_cancelled()?;
            let count = source.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            scanned += count as u64;
            if scanned > before.len() {
                return Err(io::Error::other("artifact grew while hashing"));
            }
            digest.update(&buffer[..count]);
        }
        let after = source.metadata()?;
        if scanned != before.len()
            || before.mtime() != after.mtime()
            || before.mtime_nsec() != after.mtime_nsec()
            || before.ctime() != after.ctime()
            || before.ctime_nsec() != after.ctime_nsec()
        {
            return Err(io::Error::other("artifact changed while hashing"));
        }
        let existing = hex(&digest.finalize());
        let object = self.root.join("objects").join(&existing);
        if object.exists() {
            verify_object(&object, &existing, scanned)?;
            return Ok(Artifact {
                sha256: existing,
                size: scanned,
                role: role.into(),
                path_base: base.into(),
                original_path_hex: hex(original),
            });
        }
        use std::io::Seek;
        source.rewind()?;
        self.reserve(before.len() + 4096, 1)?;
        let tmp = self.directory.join(format!(
            "artifact-{}",
            SERIAL.fetch_add(1, Ordering::Relaxed)
        ));
        let mut dest = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&tmp)?;
        let mut digest = Sha256::new();
        let mut buffer = [0u8; 8192];
        let mut size = 0u64;
        loop {
            crate::process::check_cancelled()?;
            let count = source.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            size += count as u64;
            if size > before.len() {
                return Err(io::Error::other("artifact grew while copying"));
            }
            dest.write_all(&buffer[..count])?;
            digest.update(&buffer[..count]);
        }
        let after = source.metadata()?;
        if size != before.len()
            || before.mtime() != after.mtime()
            || before.mtime_nsec() != after.mtime_nsec()
            || before.ctime() != after.ctime()
            || before.ctime_nsec() != after.ctime_nsec()
        {
            return Err(io::Error::other("artifact changed while copying"));
        }
        dest.set_times(fs::FileTimes::new().set_modified(std::time::UNIX_EPOCH))?;
        dest.sync_all()?;
        let sha256 = hex(&digest.finalize());
        let duplicate = match fs::hard_link(&tmp, self.root.join("objects").join(&sha256)) {
            Ok(()) => false,
            Err(err) if err.kind() == io::ErrorKind::AlreadyExists => true,
            Err(err) => return Err(err),
        };
        fs::remove_file(tmp)?;
        if duplicate {
            self.release(before.len() + 4096, 1)?;
        }
        Ok(Artifact {
            sha256,
            size,
            role: role.into(),
            path_base: base.into(),
            original_path_hex: hex(original),
        })
    }

    pub fn commit(&mut self, record: &serde_json::Value) -> io::Result<()> {
        let bytes = serde_json::to_vec(record)?;
        if bytes.len() as u64 > RECORD_LIMIT {
            return Err(io::Error::other("invocation record exceeds 4 MiB"));
        }
        // The initial reservation includes 64 KiB each for the record and its
        // manifest entry. Charge both copies of larger command records too.
        self.reserve((bytes.len() as u64).saturating_sub(65536) * 2, 0)?;
        atomic_write(
            &self.root.join("records").join(format!("{}.json", self.id)),
            &bytes,
        )?;
        fs::remove_dir_all(&self.directory)?;
        let _guard = Lock::acquire(&self.root)?;
        let (attempts, completed) = counters(&self.root)?;
        set_counters(&self.root, attempts, completed + 1)
    }

    pub(crate) fn pch_control(&mut self, hash: &str, data: &[u8]) -> io::Result<()> {
        if data.len() > RECORD_LIMIT as usize {
            return Err(io::Error::other(
                "PCH input index exceeds its metadata limit",
            ));
        }
        self.reserve(data.len() as u64 * 2 + 8192, 2)?;
        fs::create_dir_all(self.root.join("pch-inputs"))?;
        atomic_write(
            &self.root.join("pch-inputs").join(format!("{hash}.json")),
            data,
        )
    }

    pub fn metadata(&mut self, value: &serde_json::Value) -> io::Result<Artifact> {
        let bytes = serde_json::to_vec(value)?;
        if bytes.len() as u64 > RECORD_LIMIT {
            return Err(io::Error::other("subprocess command exceeds 4 MiB"));
        }
        self.reserve(bytes.len() as u64 + 4096, 1)?;
        let name = format!("{}.json", hash(&bytes));
        let path = self.directory.join(&name);
        atomic_write(&path, &bytes)?;
        let result = self.snapshot(&path, "command", "invocation-stage", name.as_bytes());
        fs::remove_file(path)?;
        self.release(bytes.len() as u64 + 4096, 1)?;
        result
    }

    pub fn limits(&self) -> serde_json::Value {
        serde_json::json!({"artifact_bytes":self.artifact_limit,"invocation_bytes":self.invocation_limit,"total_bytes":self.limit})
    }
}

fn counters(root: &Path) -> io::Result<(u64, u64)> {
    let mut data = [0u8; 16];
    File::open(root.join(".counters"))?.read_exact(&mut data)?;
    Ok((
        u64::from_le_bytes(data[..8].try_into().unwrap()),
        u64::from_le_bytes(data[8..].try_into().unwrap()),
    ))
}

fn set_counters(root: &Path, attempts: u64, completed: u64) -> io::Result<()> {
    use std::os::unix::fs::FileExt;
    let file = OpenOptions::new()
        .write(true)
        .open(root.join(".counters"))?;
    let data = [attempts.to_le_bytes(), completed.to_le_bytes()].concat();
    file.write_all_at(&data, 0)?;
    file.sync_all()
}

pub fn initialize(root: &Path) -> io::Result<()> {
    fs::create_dir_all(root)?;
    let _guard = Lock::acquire(root)?;
    for name in ["pending", "objects", "records"] {
        fs::create_dir(root.join(name))?;
    }
    std::os::unix::fs::symlink(AUXILIARY_VIEW, root.join("descriptor-view"))?;
    let mut file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(root.join(".counters"))?;
    file.write_all(&[0u8; 16])?;
    file.sync_all()
}

pub(crate) fn verify_object(path: &Path, expected: &str, size: u64) -> io::Result<()> {
    let mut file = File::open(path)?;
    if file.metadata()?.len() != size || size > MAX_FILE_LIMIT {
        return Err(io::Error::other("artifact size mismatch"));
    }
    let mut digest = Sha256::new();
    let mut buffer = [0u8; 8192];
    let mut count = 0u64;
    loop {
        let n = file.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        count += n as u64;
        if count > size {
            return Err(io::Error::other("artifact grew during validation"));
        }
        digest.update(&buffer[..n]);
    }
    if count != size || hex(&digest.finalize()) != expected {
        return Err(io::Error::other("artifact checksum mismatch"));
    }
    Ok(())
}

pub fn finalize(root: &Path, output: &Path, required: bool) -> io::Result<()> {
    fs::create_dir_all(output)?;
    let mut errors = Vec::new();
    let mut records: BTreeMap<String, (serde_json::Value, u64)> = BTreeMap::new();
    let mut artifacts: BTreeMap<String, u64> = BTreeMap::new();
    let mut processed = 0;
    let mut diagnosed = 0usize;
    let mut record_bytes = 0u64;
    let (attempts, completed) = counters(root)?;
    if attempts != completed {
        errors.push("invocations were lost or interrupted".into());
    }
    if root.join("pending").exists() && fs::read_dir(root.join("pending"))?.next().is_some() {
        errors.push("unfinished invocation or failed record commit".to_string());
    }
    if root.join("records").exists() {
        for entry in fs::read_dir(root.join("records"))? {
            processed += 1;
            if processed > FILE_COUNT_LIMIT {
                return Err(io::Error::other("too many invocation records"));
            }
            let entry = entry?;
            let file = File::open(entry.path())?;
            if file.metadata()?.len() > RECORD_LIMIT {
                errors.push("oversized invocation record".into());
                continue;
            }
            record_bytes += file.metadata()?.len();
            if record_bytes > MANIFEST_LIMIT as u64 {
                errors.push("invocation metadata exceeds the 16 MiB manifest limit".into());
                continue;
            }
            let record: serde_json::Value = match serde_json::from_reader(file) {
                Ok(value) => value,
                Err(_) => {
                    errors.push("invalid invocation record".into());
                    continue;
                }
            };
            if record.get("schema").and_then(|v| v.as_u64()) != Some(1) {
                errors.push("unknown invocation schema".into());
                continue;
            }
            if record.get("capture_complete").and_then(|v| v.as_bool()) != Some(true) {
                errors.push("incomplete compiler invocation".into());
                if required && diagnosed < 16 {
                    let mode = record["analysis"]["mode"].as_str().unwrap_or("unknown");
                    for error in record["capture_errors"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .take(4)
                    {
                        let diagnostic = error
                            .as_str()
                            .unwrap_or("invalid capture diagnostic")
                            .chars()
                            .take(256)
                            .collect::<String>();
                        eprintln!("compiler-capture: incomplete {mode} invocation: {diagnostic}");
                    }
                    diagnosed += 1;
                }
            }
            if let Some(items) = record.get("artifacts").and_then(|v| v.as_array()) {
                for item in items {
                    let artifact: Artifact = serde_json::from_value(item.clone())?;
                    if artifact.sha256.len() != 64
                        || !artifact
                            .sha256
                            .bytes()
                            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
                    {
                        return Err(io::Error::other("invalid artifact ID"));
                    }
                    verify_object(
                        &root.join("objects").join(&artifact.sha256),
                        &artifact.sha256,
                        artifact.size,
                    )?;
                    artifacts.insert(artifact.sha256, artifact.size);
                }
            } else {
                errors.push("missing artifact list".into());
            }
            let id = hash(&serde_json::to_vec(&record)?);
            records
                .entry(id)
                .and_modify(|entry| entry.1 += 1)
                .or_insert((record, 1));
        }
    }
    fs::create_dir(output.join("objects"))?;
    if processed != completed {
        errors.push("invocation record count does not match durable counters".into());
    }
    // Byte copies, never links to package objects. Finalization is after every
    // package phase and fixup hook, so snapshots cannot be stripped in place.
    for (id, size) in &artifacts {
        let source = root.join("objects").join(id);
        let dest = output.join("objects").join(id);
        let mut input = File::open(source)?.take(size + 1);
        let mut target = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o444)
            .open(&dest)?;
        if io::copy(&mut input, &mut target)? != *size {
            return Err(io::Error::other("artifact changed during finalization"));
        }
        target.sync_all()?;
        verify_object(&dest, id, *size)?;
    }
    errors.sort();
    errors.dedup();
    let complete = errors.is_empty();
    let manifest = serde_json::json!({"schema":1,"wrapper_revision":1,"complete":complete,
        "policy":if required {"required"} else {"observe"}, "errors":errors,
        "artifacts":artifacts,"invocations":records.into_iter().map(|(id,(record,count))|
            serde_json::json!({"id":id,"count":count,"record":record})).collect::<Vec<_>>()});
    let bytes = serde_json::to_vec(&manifest)?;
    if bytes.len() > MANIFEST_LIMIT {
        return Err(io::Error::other("manifest exceeds 16 MiB"));
    }
    atomic_write(&output.join("manifest.json"), &bytes)?;
    for entry in fs::read_dir(root)? {
        let entry = entry?;
        if entry.file_type()?.is_dir() {
            fs::remove_dir_all(entry.path())?;
        } else {
            fs::remove_file(entry.path())?;
        }
    }
    if let Err(error) = fs::remove_dir(root) {
        // A production scratch root can be a private mount point. Its parent
        // removes the empty underlying directory after that namespace exits.
        if error.raw_os_error() != Some(libc::EBUSY) {
            return Err(error);
        }
    }
    if required && !complete {
        return Err(io::Error::other("required compiler capture is incomplete"));
    }
    Ok(())
}

pub fn discard(root: &Path) -> io::Result<()> {
    let name = root
        .file_name()
        .and_then(|s| s.to_str())
        .unwrap_or_default();
    if root.parent() != Some(Path::new("/tmp")) || !name.starts_with(".dataset-capture.") {
        return Err(io::Error::other(
            "refusing to discard an unowned scratch path",
        ));
    }
    let metadata = match fs::symlink_metadata(root) {
        Ok(value) => value,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    if !metadata.is_dir() || metadata.uid() != unsafe { libc::geteuid() } {
        return Err(io::Error::other("scratch path has the wrong type or owner"));
    }
    fs::remove_dir_all(root)
}
