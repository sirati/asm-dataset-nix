use std::{
    ffi::OsString,
    io,
    os::unix::process::{CommandExt, ExitStatusExt},
    process::{Command, ExitStatus},
    sync::atomic::{AtomicI32, Ordering},
};
static CHILD: AtomicI32 = AtomicI32::new(0);
static CANCELLED: AtomicI32 = AtomicI32::new(0);

extern "C" fn forward(sig: i32) {
    CANCELLED.store(sig, Ordering::SeqCst);
    let child = CHILD.load(Ordering::SeqCst);
    if child > 0 {
        unsafe {
            libc::kill(-child, sig);
        }
    }
}

pub fn check_cancelled() -> io::Result<()> {
    if CANCELLED.load(Ordering::SeqCst) != 0 {
        Err(io::Error::new(
            io::ErrorKind::Interrupted,
            "capture cancelled",
        ))
    } else {
        Ok(())
    }
}

pub fn install_handlers() -> io::Result<()> {
    // Adopt compiler grandchildren so cancellation can wait for their actual
    // exit, including helpers that create a separate process group.
    if unsafe { libc::prctl(libc::PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) } != 0 {
        return Err(io::Error::last_os_error());
    }
    for sig in [libc::SIGINT, libc::SIGTERM, libc::SIGHUP, libc::SIGQUIT] {
        let mut inherited: libc::sigaction = unsafe { std::mem::zeroed() };
        if unsafe { libc::sigaction(sig, std::ptr::null(), &mut inherited) } != 0 {
            return Err(io::Error::last_os_error());
        }
        if inherited.sa_sigaction == libc::SIG_IGN {
            continue;
        }
        let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
        action.sa_sigaction = forward as *const () as usize;
        unsafe {
            libc::sigemptyset(&mut action.sa_mask);
        }
        if unsafe { libc::sigaction(sig, &action, std::ptr::null_mut()) } != 0 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(())
}

fn finish_cancelled_tree(root: i32) -> io::Result<()> {
    if CANCELLED.load(Ordering::SeqCst) == 0 {
        return Ok(());
    }
    unsafe {
        libc::kill(-root, libc::SIGKILL);
    }
    loop {
        let children =
            std::fs::read_to_string(format!("/proc/self/task/{}/children", std::process::id()))?;
        for pid in children
            .split_whitespace()
            .filter_map(|value| value.parse::<i32>().ok())
        {
            unsafe {
                libc::kill(pid, libc::SIGKILL);
            }
        }
        let mut status = 0;
        if unsafe { libc::waitpid(-1, &mut status, 0) } >= 0 {
            continue;
        }
        let error = io::Error::last_os_error();
        if error.raw_os_error() == Some(libc::ECHILD) {
            return Ok(());
        }
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
}

pub fn register_child(pid: i32) {
    CHILD.store(pid, Ordering::SeqCst);
    let cancelled = CANCELLED.load(Ordering::SeqCst);
    if cancelled != 0 {
        unsafe {
            libc::kill(-pid, cancelled);
        }
    }
}

pub fn child_finished() {
    CHILD.store(0, Ordering::SeqCst);
}

pub fn supervise(executable: &std::path::Path, args: &[OsString]) -> io::Result<ExitStatus> {
    install_handlers()?;
    let mut command = Command::new(executable);
    command.args(args);
    unsafe {
        command.pre_exec(|| {
            crate::signals::restore_sigpipe();
            if libc::setpgid(0, 0) != 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut child = command.spawn()?;
    register_child(child.id() as i32);
    let status = child.wait()?;
    // A terminated compiler may leave a subprocess in its group. Ensure that
    // cancellation reaches the whole compiler tree before returning its status.
    finish_cancelled_tree(child.id() as i32)?;
    child_finished();
    Ok(status)
}

pub fn pump_stdout<R, F>(mut source: R, pid: i32, mut collect: F) -> io::Result<Option<String>>
where
    R: std::io::Read,
    F: FnMut(&[u8]) -> io::Result<()>,
{
    let mut mask: libc::sigset_t = unsafe { std::mem::zeroed() };
    unsafe {
        libc::sigemptyset(&mut mask);
        libc::sigaddset(&mut mask, libc::SIGPIPE);
        libc::pthread_sigmask(libc::SIG_BLOCK, &mask, std::ptr::null_mut());
    }
    let mut error = None;
    let mut buffer = [0u8; 8192];
    loop {
        let count = match source.read(&mut buffer) {
            Ok(count) => count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(error),
        };
        if count == 0 {
            break;
        }
        if error.is_none() {
            if let Err(value) = collect(&buffer[..count]) {
                error = Some(value.to_string());
            }
        }
        let mut offset = 0;
        while offset < count {
            let written = unsafe {
                libc::write(
                    libc::STDOUT_FILENO,
                    buffer[offset..count].as_ptr().cast(),
                    count - offset,
                )
            };
            if written < 0 {
                let error = io::Error::last_os_error();
                if error.kind() == io::ErrorKind::Interrupted {
                    continue;
                }
                unsafe {
                    libc::kill(-pid, libc::SIGPIPE);
                }
                return Err(error);
            }
            if written == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::WriteZero,
                    "stdout forwarding stopped",
                ));
            }
            offset += written as usize;
        }
    }
    Ok(error)
}

pub fn stdout_capture<F>(
    executable: &std::path::Path,
    args: &[OsString],
    collect: F,
) -> io::Result<(ExitStatus, Option<String>)>
where
    F: FnMut(&[u8]) -> io::Result<()> + Send,
{
    install_handlers()?;
    let mut command = Command::new(executable);
    command.args(args).stdout(std::process::Stdio::piped());
    unsafe {
        command.pre_exec(|| {
            crate::signals::restore_sigpipe();
            if libc::setpgid(0, 0) != 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let mut child = command.spawn()?;
    let pid = child.id() as i32;
    register_child(pid);
    let source = child.stdout.take().unwrap();
    let (status, error) = std::thread::scope(|scope| {
        let pump = scope.spawn(move || pump_stdout(source, pid, collect));
        let status = child.wait();
        let cleanup = finish_cancelled_tree(pid);
        let error = match pump.join() {
            Ok(Ok(error)) => error,
            Ok(Err(error)) => Some(error.to_string()),
            Err(_) => Some("stdout capture thread failed".into()),
        };
        (status.and_then(|value| cleanup.map(|_| value)), error)
    });
    child_finished();
    Ok((status?, error))
}

pub fn exit_like(status: ExitStatus) -> ! {
    let sig = CANCELLED.load(Ordering::SeqCst);
    if let Some(signal) = status.signal().or(if sig != 0 { Some(sig) } else { None }) {
        unsafe {
            libc::signal(signal, libc::SIG_DFL);
            let mut mask: libc::sigset_t = std::mem::zeroed();
            libc::sigemptyset(&mut mask);
            libc::sigaddset(&mut mask, signal);
            libc::sigprocmask(libc::SIG_UNBLOCK, &mask, std::ptr::null_mut());
            libc::raise(signal);
        }
        std::process::exit(128 + signal);
    }
    std::process::exit(status.code().unwrap_or(125));
}
