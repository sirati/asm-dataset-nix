mod arguments;
mod collect;
mod config;
mod gcc_options;
mod isolate;
mod pch;
mod process;
mod response;
mod signals;
mod storage;
#[cfg(target_arch = "x86_64")]
mod trace;

use std::{env, ffi::OsString, io, os::unix::process::CommandExt, path::PathBuf, process::Command};

fn run() -> io::Result<()> {
    let mut args = env::args_os();
    let _entry = args.next();
    let first = args.next().unwrap_or_default();
    if first == "discard" {
        let root = PathBuf::from(
            args.next()
                .ok_or_else(|| io::Error::other("missing scratch directory"))?,
        );
        return storage::discard(&root);
    }
    if first == "isolate" {
        let root = PathBuf::from(
            args.next()
                .ok_or_else(|| io::Error::other("missing staging directory"))?,
        );
        let limit = args
            .next()
            .and_then(|a| a.to_str().and_then(|s| s.parse().ok()))
            .ok_or_else(|| io::Error::other("missing scratch limit"))?;
        let program = args
            .next()
            .ok_or_else(|| io::Error::other("missing package builder"))?;
        return isolate::exec(&root, limit, program, args);
    }
    if first == "init" {
        let root = PathBuf::from(
            args.next()
                .ok_or_else(|| io::Error::other("missing staging directory"))?,
        );
        return storage::initialize(&root);
    }
    if first == "finalize" {
        let root = PathBuf::from(
            args.next()
                .ok_or_else(|| io::Error::other("missing staging directory"))?,
        );
        let output = PathBuf::from(
            args.next()
                .ok_or_else(|| io::Error::other("missing capture output"))?,
        );
        let policy = args
            .next()
            .ok_or_else(|| io::Error::other("missing capture policy"))?;
        if policy != "observe" && policy != "required" {
            return Err(io::Error::other("invalid capture policy"));
        }
        return storage::finalize(&root, &output, policy == "required");
    }
    if first != "--config" {
        return Err(io::Error::other(
            "usage: compiler-capture --config FILE -- ARGS...",
        ));
    }
    let path = PathBuf::from(
        args.next()
            .ok_or_else(|| io::Error::other("missing configuration"))?,
    );
    if args.next().as_deref() != Some(std::ffi::OsStr::new("--")) {
        return Err(io::Error::other("missing argument delimiter"));
    }
    let config = config::Config::load(&path)?;
    let original: Vec<OsString> = args.collect();
    let already_traced = env::var("DATASET_CAPTURE_TRACER")
        .ok()
        .is_some_and(|expected| {
            expected != "0"
                && std::fs::read_to_string("/proc/self/status")
                    .ok()
                    .is_some_and(|status| {
                        status.lines().any(|line| {
                            line.strip_prefix("TracerPid:")
                                .is_some_and(|value| value.trim() == expected)
                        })
                    })
        });
    if let Some(root) = env::var_os("DATASET_CAPTURE_STAGE").filter(|_| !already_traced) {
        process::exit_like(collect::run(&config, &original, &PathBuf::from(root))?);
    }
    // Forwarding is exec, so descriptors, process groups, signal dispositions,
    // stdin, diagnostics and wait status retain the normal driver's behavior.
    let mut command = Command::new(&config.executable);
    command.args(original);
    unsafe {
        command.pre_exec(|| {
            signals::restore_sigpipe();
            Ok(())
        });
    }
    let error = command.exec();
    Err(error)
}

fn main() {
    signals::restore_sigpipe();
    if let Err(error) = run() {
        eprintln!("compiler-capture: {error}");
        std::process::exit(125);
    }
}
