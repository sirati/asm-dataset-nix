//! Bound generated intermediates before the package builder starts. Keep the
//! builder's numeric UID/GID. Mount before exec so its capabilities then drop
//! under the usual exec rules for the original non-root UID.
use std::{
    ffi::{CString, OsString},
    fs, io,
    os::unix::{ffi::OsStrExt, process::CommandExt},
    path::Path,
    process::Command,
};

pub fn exec(
    root: &Path,
    limit: u64,
    program: OsString,
    args: impl Iterator<Item = OsString>,
) -> io::Result<()> {
    if !root.is_absolute() || !(64 * 1024 * 1024..=2 * 1024 * 1024 * 1024).contains(&limit) {
        return Err(io::Error::other("invalid capture scratch mount or size"));
    }
    {
        let uid = unsafe { libc::geteuid() };
        let gid = unsafe { libc::getegid() };
        if unsafe { libc::unshare(libc::CLONE_NEWUSER | libc::CLONE_NEWNS) } != 0 {
            return Err(io::Error::other(format!(
                "capture scratch namespace: {}",
                io::Error::last_os_error()
            )));
        }
        fs::write("/proc/self/uid_map", format!("{uid} {uid} 1\n"))?;
        fs::write("/proc/self/setgroups", "deny\n")?;
        fs::write("/proc/self/gid_map", format!("{gid} {gid} 1\n"))?;
        let target = CString::new(root.as_os_str().as_bytes())?;
        let options = CString::new(format!(
            "size={limit},nr_inodes=16384,mode=0700,uid={uid},gid={gid}"
        ))?;
        if unsafe {
            libc::mount(
                c"tmpfs".as_ptr(),
                target.as_ptr(),
                c"tmpfs".as_ptr(),
                libc::MS_NOSUID | libc::MS_NODEV | libc::MS_NOEXEC,
                options.as_ptr().cast(),
            )
        } != 0
        {
            return Err(io::Error::other(format!(
                "capture scratch mount: {}",
                io::Error::last_os_error()
            )));
        }
    }
    let mut command = Command::new(program);
    command.args(args);
    unsafe {
        command.pre_exec(|| {
            crate::signals::restore_sigpipe();
            Ok(())
        });
    }
    Err(command.exec())
}
