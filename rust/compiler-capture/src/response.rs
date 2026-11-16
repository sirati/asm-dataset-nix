//! Old Nix wrappers pass a printf pipe as @/dev/fd/N. Reading that pipe for
//! analysis would consume the compiler's arguments. Retain its bytes in owned
//! storage, with a streaming fallback if analysis/storage limits are reached.
use std::{
    ffi::{OsStr, OsString},
    fs::{self, File},
    io::{self, Read, Write},
    os::{
        fd::{AsRawFd, FromRawFd},
        unix::{ffi::OsStrExt, fs::FileTypeExt},
    },
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
};

const LIMIT: usize = 1024 * 1024;

pub enum Prepared {
    Original,
    Buffered {
        arguments: Vec<OsString>,
        path: PathBuf,
    },
    Streaming {
        source: File,
        prefix: Vec<u8>,
        error: String,
    },
}

pub fn prepare(arguments: &[OsString], directory: &Path, enabled: bool) -> io::Result<Prepared> {
    let enabled = std::env::var("NIX_CC_USE_RESPONSE_FILE")
        .ok()
        .map(|s| s.parse::<u32>().unwrap_or(0) > 0)
        .unwrap_or(enabled);
    if !enabled || arguments.len() != 1 {
        return Ok(Prepared::Original);
    }
    let Some(path) = arguments[0].as_bytes().strip_prefix(b"@/dev/fd/") else {
        return Ok(Prepared::Original);
    };
    if path.is_empty() || !path.iter().all(u8::is_ascii_digit) {
        return Ok(Prepared::Original);
    }
    let path = Path::new(OsStr::from_bytes(&arguments[0].as_bytes()[1..]));
    let Ok(mut source) = File::open(path) else {
        return Ok(Prepared::Original);
    };
    if !source.metadata()?.file_type().is_fifo() {
        return Ok(Prepared::Original);
    }
    let mut prefix = Vec::new();
    let mut buffer = [0u8; 8192];
    loop {
        let remaining = (LIMIT + 1 - prefix.len()).min(buffer.len());
        match source.read(&mut buffer[..remaining]) {
            Ok(0) => break,
            Ok(n) => prefix.extend_from_slice(&buffer[..n]),
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => {
                return Ok(Prepared::Streaming {
                    source,
                    prefix,
                    error: format!("wrapper response read: {error}"),
                })
            }
        }
        if prefix.len() > LIMIT {
            return Ok(Prepared::Streaming {
                source,
                prefix,
                error: "wrapper response exceeds 1 MiB analysis limit".into(),
            });
        }
    }
    let destination = directory.join("wrapper-response.rsp");
    if let Err(error) = fs::write(&destination, &prefix) {
        let _ = fs::remove_file(&destination);
        return Ok(Prepared::Streaming {
            source,
            prefix,
            error: format!("wrapper response storage: {error}"),
        });
    }
    let mut argument = OsString::from("@");
    argument.push(&destination);
    Ok(Prepared::Buffered {
        arguments: vec![argument],
        path: destination,
    })
}

pub fn forward(
    executable: &Path,
    mut source: File,
    prefix: Vec<u8>,
) -> io::Result<std::process::ExitStatus> {
    let mut descriptors = [0; 2];
    if unsafe { libc::pipe2(descriptors.as_mut_ptr(), libc::O_CLOEXEC) } < 0 {
        return Err(io::Error::last_os_error());
    }
    let reader = unsafe { File::from_raw_fd(descriptors[0]) };
    let mut writer = unsafe { File::from_raw_fd(descriptors[1]) };
    if unsafe { libc::fcntl(reader.as_raw_fd(), libc::F_SETFD, 0) } < 0 {
        return Err(io::Error::last_os_error());
    }
    let done = Arc::new(AtomicBool::new(false));
    let stop = done.clone();
    let pump = std::thread::spawn(move || -> io::Result<()> {
        // A closed consumer is an ordinary BrokenPipe in this helper thread.
        unsafe {
            let mut set = std::mem::zeroed();
            libc::sigemptyset(&mut set);
            libc::sigaddset(&mut set, libc::SIGPIPE);
            libc::pthread_sigmask(libc::SIG_BLOCK, &set, std::ptr::null_mut());
        }
        writer.write_all(&prefix)?;
        let mut buffer = [0u8; 8192];
        while !stop.load(Ordering::Relaxed) {
            let mut poll = libc::pollfd {
                fd: source.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            };
            let ready = unsafe { libc::poll(&mut poll, 1, 100) };
            if ready < 0 {
                if io::Error::last_os_error().kind() == io::ErrorKind::Interrupted {
                    continue;
                }
                return Err(io::Error::last_os_error());
            }
            if ready == 0 {
                continue;
            }
            let count = source.read(&mut buffer)?;
            if count == 0 {
                break;
            }
            writer.write_all(&buffer[..count])?;
        }
        Ok(())
    });
    let argument = OsString::from(format!("@/proc/self/fd/{}", reader.as_raw_fd()));
    let result = crate::process::supervise(executable, &[argument]);
    done.store(true, Ordering::Relaxed);
    drop(reader);
    // The compiler's wait status is authoritative, including when it declines
    // to read the response. The helper never changes that status.
    let _ = pump.join();
    result
}
