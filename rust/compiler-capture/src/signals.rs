//! Rust's runtime ignores SIGPIPE before main. Remember the exec-inherited
//! disposition first so forwarding does not change a compiler's pipe behavior.
use std::sync::atomic::{AtomicBool, Ordering};
static PIPE_IGNORED: AtomicBool = AtomicBool::new(false);

extern "C" fn remember_sigpipe() {
    let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
    if unsafe { libc::sigaction(libc::SIGPIPE, std::ptr::null(), &mut action) } == 0 {
        PIPE_IGNORED.store(action.sa_sigaction == libc::SIG_IGN, Ordering::Relaxed);
    }
}

#[used]
#[link_section = ".init_array"]
static BEFORE_RUST_RUNTIME: extern "C" fn() = remember_sigpipe;

pub fn restore_sigpipe() {
    unsafe {
        libc::signal(
            libc::SIGPIPE,
            if PIPE_IGNORED.load(Ordering::Relaxed) {
                libc::SIG_IGN
            } else {
                libc::SIG_DFL
            },
        );
    }
}
