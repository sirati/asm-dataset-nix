//! Linux/x86-64 experiment: redirect compiler-owned auxiliary outputs at exec
//! stops. Sources and primary output paths are never replaced. Compiler
//! subprocesses keep their executable, argv[0], descriptors, cwd and streams.
use crate::{
    process,
    storage::{self, Artifact, Invocation},
};
use serde_json::json;
use std::{
    collections::{HashMap, HashSet},
    ffi::{OsStr, OsString},
    fs, io,
    os::unix::{
        ffi::{OsStrExt, OsStringExt},
        process::{CommandExt, ExitStatusExt},
    },
    path::{Path, PathBuf},
    process::{Command, ExitStatus},
    sync::Mutex,
};

// Linux limits argv+env to at most 6 MiB. Keep analysis bounded above that
// kernel limit so a valid exec cannot become a failed compiler feature probe.
const MAX_ARGUMENT_BYTES: usize = 8 * 1024 * 1024;
const MAX_COMMANDS: usize = 128;

fn llvm_lto_suffix(suffix: &[u8]) -> bool {
    if let Some(member) = suffix.strip_prefix(b".llvm.") {
        let Some(dot) = member.iter().position(|byte| *byte == b'.') else {
            return false;
        };
        return dot > 0
            && member[..dot].iter().all(u8::is_ascii_digit)
            && !member[dot + 1..].contains(&b'/')
            && [b".thinlto.o".as_slice(), b".opt.bc", b".bc"]
                .iter()
                .any(|ending| member[dot + 1..].ends_with(ending));
    }
    suffix.starts_with(b".0.") && suffix.ends_with(b".bc")
        || [
            b".1.promote.bc".as_slice(),
            b".2.internalize.bc",
            b".3.import.bc",
            b".4.opt.bc",
            b".5.precodegen.bc",
            b".index.bc",
            b".index.dot",
            b".resolution.txt",
            b".bc",
            b".opt.bc",
            b".thinlto.bc",
            b".thinlto.o",
            b".lto_wrapper_args",
            b".o",
        ]
        .contains(&suffix)
        || suffix.starts_with(b".lto.o")
        || suffix
            .strip_suffix(b".lto.o")
            .is_some_and(|digits| !digits.is_empty() && digits.iter().all(u8::is_ascii_digit))
        || suffix
            .strip_prefix(b".o")
            .is_some_and(|tail| !tail.is_empty() && tail.iter().all(u8::is_ascii_digit))
}

fn compiler_view(view: &Path, path: &Path) -> Vec<u8> {
    view.join(path.file_name().expect("auxiliary file has a name"))
        .into_os_string()
        .into_vec()
}

fn ptrace(
    request: libc::c_uint,
    pid: i32,
    address: usize,
    data: usize,
) -> io::Result<libc::c_long> {
    unsafe {
        *libc::__errno_location() = 0;
    }
    let value = unsafe {
        libc::ptrace(
            request,
            pid,
            address as *mut libc::c_void,
            data as *mut libc::c_void,
        )
    };
    if value == -1 && unsafe { *libc::__errno_location() } != 0 {
        let error = io::Error::last_os_error();
        if error.raw_os_error() == Some(libc::ESRCH) {
            return Err(error);
        }
        Err(io::Error::other(format!(
            "ptrace request {request:#x}: {}",
            error
        )))
    } else {
        Ok(value)
    }
}

fn wait(pid: i32) -> io::Result<(i32, i32)> {
    loop {
        let mut status = 0;
        let result = unsafe { libc::waitpid(pid, &mut status, libc::__WALL) };
        if result >= 0 {
            return Ok((result, status));
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
}

fn registers(pid: i32) -> io::Result<libc::user_regs_struct> {
    let mut regs = unsafe { std::mem::zeroed() };
    ptrace(libc::PTRACE_GETREGS, pid, 0, &mut regs as *mut _ as usize)?;
    Ok(regs)
}

fn set_registers(pid: i32, regs: &libc::user_regs_struct) -> io::Result<()> {
    ptrace(libc::PTRACE_SETREGS, pid, 0, regs as *const _ as usize).map(|_| ())
}

fn word(pid: i32, address: u64) -> io::Result<u64> {
    ptrace(libc::PTRACE_PEEKDATA, pid, address as usize, 0).map(|v| v as u64)
}

fn bytes(pid: i32, address: u64, budget: usize) -> io::Result<Vec<u8>> {
    let mut result = Vec::new();
    let mut next = address;
    while result.len() < budget {
        let aligned = next & !7;
        let data = word(pid, aligned)?.to_ne_bytes();
        for &byte in &data[(next - aligned) as usize..] {
            if byte == 0 {
                return Ok(result);
            }
            result.push(byte);
            next += 1;
            if result.len() == budget {
                break;
            }
        }
    }
    Err(io::Error::other(
        "trace argument exceeds its analysis limit",
    ))
}

fn write_bytes(pid: i32, address: u64, data: &[u8]) -> io::Result<()> {
    for (index, chunk) in data.chunks(8).enumerate() {
        let target = address + (index * 8) as u64;
        let mut value = if chunk.len() == 8 {
            [0u8; 8]
        } else {
            word(pid, target)?.to_ne_bytes()
        };
        value[..chunk.len()].copy_from_slice(chunk);
        ptrace(
            libc::PTRACE_POKEDATA,
            pid,
            target as usize,
            u64::from_ne_bytes(value) as usize,
        )?;
    }
    Ok(())
}

fn arguments(pid: i32) -> io::Result<(libc::user_regs_struct, Vec<Vec<u8>>)> {
    let regs = registers(pid)?;
    if regs.cs != 0x33 {
        return Err(io::Error::other(
            "only x86-64 compiler processes have been verified",
        ));
    }
    let count = word(pid, regs.rsp)? as usize;
    if count > 1024 * 1024 {
        return Err(io::Error::other("too many traced arguments"));
    }
    let mut result = Vec::new();
    let mut budget = MAX_ARGUMENT_BYTES;
    for index in 0..count {
        let address = word(pid, regs.rsp + 8 * (index as u64 + 1))?;
        let arg = bytes(pid, address, budget)?;
        budget = budget.saturating_sub(arg.len() + 1);
        result.push(arg);
    }
    Ok((regs, result))
}

// Allocate data in the stopped child before its dynamic loader runs. Only
// argument strings live here. Arity-preserving rewrites change argv pointers;
// resized argv tables retain the original stack and envp/auxv values below.
fn remote_memory(pid: i32, length: usize) -> io::Result<u64> {
    let saved = registers(pid)?;
    let instruction = word(pid, saved.rip)?;
    let patched = (instruction & !0xffffff) | 0xcc050f; // syscall; int3
    ptrace(
        libc::PTRACE_POKETEXT,
        pid,
        saved.rip as usize,
        patched as usize,
    )?;
    let result = (|| {
        let mut regs = saved;
        regs.rax = libc::SYS_mmap as u64;
        regs.rdi = 0;
        regs.rsi = (length.div_ceil(4096) * 4096) as u64;
        regs.rdx = (libc::PROT_READ | libc::PROT_WRITE) as u64;
        regs.r10 = (libc::MAP_PRIVATE | libc::MAP_ANONYMOUS) as u64;
        regs.r8 = u64::MAX;
        regs.r9 = 0;
        set_registers(pid, &regs)?;
        ptrace(libc::PTRACE_CONT, pid, 0, 0)?;
        let (_, status) = wait(pid)?;
        if !libc::WIFSTOPPED(status) || libc::WSTOPSIG(status) != libc::SIGTRAP || status >> 16 != 0
        {
            return Err(io::Error::other(
                "child interrupted during argument allocation",
            ));
        }
        let address = registers(pid)?.rax;
        if (address as i64) < 0 {
            return Err(io::Error::other("child argument allocation failed"));
        }
        Ok(address)
    })();
    ptrace(
        libc::PTRACE_POKETEXT,
        pid,
        saved.rip as usize,
        instruction as usize,
    )?;
    set_registers(pid, &saved)?;
    result
}

fn replace_arguments(
    pid: i32,
    stack: u64,
    original: &[Vec<u8>],
    rewritten: &[Vec<u8>],
) -> io::Result<u64> {
    let length = rewritten
        .iter()
        .enumerate()
        .filter(|(index, arg)| original.get(*index) != Some(*arg))
        .map(|(_, arg)| arg.len() + 1)
        .sum::<usize>();
    if length > MAX_ARGUMENT_BYTES {
        return Err(io::Error::other("rewritten arguments exceed 8 MiB"));
    }
    let memory = if length > 0 {
        remote_memory(pid, length)?
    } else {
        0
    };
    let mut pointers = Vec::with_capacity(rewritten.len());
    let mut offset = 0u64;
    for (index, arg) in rewritten.iter().enumerate() {
        if original.get(index) == Some(arg) {
            pointers.push(word(pid, stack + 8 * (index as u64 + 1))?);
        } else {
            let address = memory + offset;
            let mut bytes = arg.clone();
            bytes.push(0);
            write_bytes(pid, address, &bytes)?;
            pointers.push(address);
            offset += bytes.len() as u64;
        }
    }
    if original.len() == rewritten.len() {
        for (index, pointer) in pointers.iter().enumerate() {
            ptrace(
                libc::PTRACE_POKEDATA,
                pid,
                (stack + 8 * (index as u64 + 1)) as usize,
                *pointer as usize,
            )?;
        }
        return Ok(stack);
    }
    // GCC 4.4 cc1 does not expand response files. Resize its initial argv
    // table on the existing stack, retaining the exact envp and auxv values
    // and all original string addresses. No replacement stack is installed.
    let mut cursor = stack + 8 * (original.len() as u64 + 2);
    let mut tail = Vec::new();
    let mut terminated = false;
    for _ in 0..65536 {
        let value = word(pid, cursor)?;
        cursor += 8;
        tail.push(value);
        if value == 0 {
            terminated = true;
            break;
        }
    }
    if !terminated {
        return Err(io::Error::other(
            "frontend environment table exceeds its limit",
        ));
    }
    terminated = false;
    for _ in 0..256 {
        let kind = word(pid, cursor)?;
        let value = word(pid, cursor + 8)?;
        cursor += 16;
        tail.extend([kind, value]);
        if kind == 0 {
            terminated = true;
            break;
        }
    }
    if !terminated {
        return Err(io::Error::other(
            "frontend auxiliary vector exceeds its limit",
        ));
    }
    let mut header = vec![rewritten.len() as u64];
    header.extend(pointers);
    header.push(0);
    header.extend(tail);
    let bytes = header
        .into_iter()
        .flat_map(u64::to_ne_bytes)
        .collect::<Vec<_>>();
    let old_length = (cursor - stack) as usize;
    let growth = bytes.len().saturating_sub(old_length).div_ceil(16) * 16;
    let new_stack = stack
        .checked_sub(growth as u64)
        .ok_or_else(|| io::Error::other("frontend stack address overflow"))?;
    let mapped = fs::read_to_string(format!("/proc/{pid}/maps"))?
        .lines()
        .any(|line| {
            let mut fields = line.split_ascii_whitespace();
            let Some(range) = fields.next().and_then(|field| field.split_once('-')) else {
                return false;
            };
            let Some(permissions) = fields.next() else {
                return false;
            };
            let (Ok(first), Ok(last)) = (
                u64::from_str_radix(range.0, 16),
                u64::from_str_radix(range.1, 16),
            ) else {
                return false;
            };
            permissions.contains('w')
                && first <= new_stack
                && new_stack + bytes.len() as u64 <= last
        });
    if !mapped {
        return Err(io::Error::other(
            "frontend argument table exceeds its mapped stack",
        ));
    }
    write_bytes(pid, new_stack, &bytes)?;
    let mut regs = registers(pid)?;
    regs.rsp = new_stack;
    set_registers(pid, &regs)?;
    Ok(new_stack)
}

fn gcc_auxiliary_environment(
    pid: i32,
    stack: u64,
    argc: usize,
    output: Option<&Path>,
) -> io::Result<Option<serde_json::Value>> {
    let mut budget = MAX_ARGUMENT_BYTES;
    for index in 0..65536 {
        let slot = stack + 8 * (argc as u64 + 2 + index);
        let address = word(pid, slot)?;
        if address == 0 {
            return Ok(None);
        }
        let value = bytes(pid, address, budget)?;
        budget = budget.saturating_sub(value.len() + 1);
        if let Some(options) = value.strip_prefix(b"COLLECT_GCC_OPTIONS=") {
            let rewritten = if let Some(output) = output {
                crate::gcc_options::auxiliary_output(options, output.as_os_str().as_bytes())?
            } else {
                crate::gcc_options::deduplicate_dumpdir(options)?
            };
            if rewritten == options {
                return Ok(None);
            }
            let mut data = b"COLLECT_GCC_OPTIONS=".to_vec();
            data.extend_from_slice(&rewritten);
            data.push(0);
            if data.len() > MAX_ARGUMENT_BYTES {
                return Err(io::Error::other(
                    "GCC auxiliary environment exceeds its limit",
                ));
            }
            let memory = remote_memory(pid, data.len())?;
            write_bytes(pid, memory, &data)?;
            ptrace(libc::PTRACE_POKEDATA, pid, slot as usize, memory as usize)?;
            return Ok(Some(
                json!({"variable":"COLLECT_GCC_OPTIONS","original_value_hex":storage::hex(options),"effective_value_hex":storage::hex(&rewritten),"scope":if output.is_some(){"lto-auxiliary-output-prefix"}else{"deduplicate-effective-dumpdir"}}),
            ));
        }
    }
    Err(io::Error::other("too many environment entries"))
}

fn absolute(cwd: &Path, bytes: &[u8]) -> Vec<u8> {
    let path = cwd.join(OsStr::from_bytes(bytes));
    if let (Some(parent), Some(name)) = (path.parent(), path.file_name()) {
        if let Ok(parent) = fs::canonicalize(parent) {
            return parent.join(name).into_os_string().into_vec();
        }
    }
    path.into_os_string().into_vec()
}

fn normalized(arg: &[u8], aux: &Path) -> String {
    // collect2 reserves a random resolution filename even for ordinary
    // non-LTO links. It is a compiler-owned temporary operand, like a Nix
    // response filename, and must not become canonical invocation identity.
    for leader in [b"-plugin-opt=-fresolution=".as_slice(), b"-fresolution="] {
        if let Some(path) = arg.strip_prefix(leader) {
            let path = Path::new(OsStr::from_bytes(path));
            let temporary = std::env::var_os("TMPDIR")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("/tmp"));
            if path.parent() == Some(temporary.as_path())
                && path.file_name().is_some_and(|name| {
                    let name = name.as_bytes();
                    name.len() == 12
                        && name.starts_with(b"cc")
                        && name.ends_with(b".res")
                        && name[2..8].iter().all(u8::is_ascii_alphanumeric)
                })
            {
                return storage::hex(&[leader, b"${GCC_RESOLUTION_FILE}"].concat());
            }
        }
    }
    let prefix = aux.as_os_str().as_bytes();
    let (leader, arg) = if let Some(rest) = arg.strip_prefix(b"@") {
        (b"@".as_slice(), rest)
    } else {
        (b"".as_slice(), arg)
    };
    if let Some(rest) = arg.strip_prefix(prefix) {
        storage::hex(&[leader, b"${INVOCATION_STAGE}".as_slice(), rest].concat())
    } else {
        storage::hex(&[leader, arg].concat())
    }
}

#[derive(Clone)]
struct Output {
    path: PathBuf,
    original: Vec<u8>,
    role: &'static str,
    staged: bool,
}

#[derive(Default)]
struct Task {
    execs: Vec<serde_json::Value>,
    output: Option<Output>,
    driver: bool,
    path_memory: Option<u64>,
    path_pool: Option<u64>,
    modules: bool,
    module_paths: HashSet<PathBuf>,
    lto_wpa: bool,
    headers: bool,
    source_alias: Option<SourceAlias>,
}

#[derive(Clone)]
struct SourceAlias {
    original: Vec<u8>,
    saved: PathBuf,
}

struct NativeBackend {
    arguments: Vec<Vec<u8>>,
    input_index: usize,
}

type Rewritten = (Vec<Vec<u8>>, Option<Output>, Option<SourceAlias>);

pub struct Outcome {
    pub status: ExitStatus,
    pub lto_started: bool,
    pub pch_inputs: Vec<crate::pch::InputState>,
    pub artifacts: Vec<Artifact>,
    pub subprocesses: Vec<serde_json::Value>,
    pub errors: Vec<String>,
    pub transformations: Vec<serde_json::Value>,
}

#[derive(Clone, Copy, PartialEq)]
pub enum CfiCorrection {
    DirectiveOrder,
    Arm45Sections,
    RiscvAlignment,
    RiscvNoRelax,
}
impl CfiCorrection {
    fn kind(self) -> &'static str {
        match self {
            Self::DirectiveOrder => "clang-cfi-directive-order",
            Self::Arm45Sections => "gcc-4.5-arm-debug-cfi-sections",
            Self::RiscvAlignment => "clang-riscv-saved-text-alignment",
            Self::RiscvNoRelax => "clang14-riscv-saved-text-no-relax",
        }
    }
}

pub struct Options<'a> {
    pub primaries: &'a [PathBuf],
    pub nix_response_default: bool,
    pub primary_role: &'static str,
    pub redirect: bool,
    pub view: &'a Path,
    pub stdout_role: Option<&'a str>,
    pub correct_cfi_sections: Option<CfiCorrection>,
    pub legacy_lto: bool,
    pub redirect_debug_objects: bool,
    pub lto_inputs: &'a [Vec<u8>],
    pub pch_sources: &'a [Artifact],
    pub pch_producer: bool,
    pub split_dwarf: bool,
    pub normalize_directives_only: bool,
    pub gcc_directives_only: bool,
    pub root_linker: bool,
    pub x86_tuning: bool,
    pub native_from_ir: bool,
}

struct Mapper {
    paths: HashMap<Vec<u8>, Vec<Output>>,
    primary: HashMap<Vec<u8>, &'static str>,
    aux: PathBuf,
    view: PathBuf,
    redirect: bool,
    module_maps: HashSet<PathBuf>,
    correct_cfi_sections: Option<CfiCorrection>,
    corrections: HashMap<PathBuf, PathBuf>,
    legacy_lto: bool,
    lto_started: bool,
    linker_generated: HashSet<Vec<u8>>,
    redirect_debug_objects: bool,
    lto_inputs: Vec<Vec<u8>>,
    pch_sources: HashMap<Vec<u8>, PathBuf>,
    normalize_directives_only: bool,
    gcc_directives_only: bool,
    preprocessing_sources: HashMap<PathBuf, Vec<u8>>,
    pch_preprocessing: HashMap<PathBuf, Vec<Vec<u8>>>,
    pch_producer: bool,
    x86_tuning: bool,
    x86_nop_features: HashMap<PathBuf, Vec<Vec<u8>>>,
    native_from_ir: bool,
    native_backends: HashMap<PathBuf, NativeBackend>,
}

fn module_path(pid: i32) -> io::Result<Option<PathBuf>> {
    let regs = registers(pid)?;
    let (address, directory) = match regs.orig_rax as i64 {
        libc::SYS_open => (regs.rdi, libc::AT_FDCWD),
        libc::SYS_openat => (regs.rsi, regs.rdi as i32),
        libc::SYS_rename => (regs.rsi, libc::AT_FDCWD),
        libc::SYS_renameat | libc::SYS_renameat2 => (regs.r10, regs.rdx as i32),
        _ => return Ok(None),
    };
    let path = bytes(pid, address, 4096)?;
    if ![
        b".pcm".as_slice(),
        b".gcm",
        b".pch",
        b".gch",
        b".modulemap",
        b".dwo",
    ]
    .iter()
    .any(|extension| path.ends_with(extension))
    {
        return Ok(None);
    }
    let base = if directory == libc::AT_FDCWD {
        fs::read_link(format!("/proc/{pid}/cwd"))?
    } else {
        fs::read_link(format!("/proc/{pid}/fd/{directory}"))?
    };
    Ok(Some(PathBuf::from(OsStr::from_bytes(&absolute(
        &base, &path,
    )))))
}

fn header_input(pid: i32, root: &Path) -> io::Result<Option<crate::pch::InputState>> {
    let regs = registers(pid)?;
    let (address, directory, flags) = match regs.orig_rax as i64 {
        libc::SYS_open => (regs.rdi, libc::AT_FDCWD, regs.rsi as i32),
        libc::SYS_openat => (regs.rsi, regs.rdi as i32, regs.rdx as i32),
        _ => return Ok(None),
    };
    if flags & (libc::O_WRONLY | libc::O_RDWR | libc::O_CREAT | libc::O_TRUNC | libc::O_PATH) != 0 {
        return Ok(None);
    }
    let name = bytes(pid, address, 4096)?;
    let base = if directory == libc::AT_FDCWD {
        fs::read_link(format!("/proc/{pid}/cwd"))?
    } else {
        fs::read_link(format!("/proc/{pid}/fd/{directory}"))?
    };
    let path = PathBuf::from(OsStr::from_bytes(&absolute(&base, &name)));
    if path.starts_with(root)
        || ["/proc", "/sys", "/dev"]
            .iter()
            .any(|prefix| path.starts_with(prefix))
    {
        return Ok(None);
    }
    crate::pch::state(&path)
}

fn output_role(path: &Path, args: &[Vec<u8>]) -> &'static str {
    if args.iter().any(|arg| arg == b"-E") {
        return "preprocessing";
    }
    if args.iter().any(|arg| arg == b"-emit-llvm-bc") {
        return "ir";
    }
    match path.extension().map(OsStr::as_bytes) {
        Some(b"i" | b"ii" | b"iim") => "preprocessing",
        Some(b"pcm" | b"gcm") => "module",
        Some(b"s" | b"S") if args.iter().any(|arg| arg == b"-E") => "preprocessing",
        Some(b"s" | b"S") => "assembly",
        Some(b"bc" | b"ll") => "ir",
        Some(b"o" | b"obj") => "object",
        _ if args
            .iter()
            .any(|argument| argument == b"-r" || argument == b"--relocatable") =>
        {
            "object"
        }
        _ => "auxiliary",
    }
}

fn pch_frontend(preprocessor: &[Vec<u8>], frontend: &[Vec<u8>]) -> Vec<Vec<u8>> {
    let mut result = Vec::new();
    let mut index = 0;
    while index < preprocessor.len() {
        match preprocessor[index].as_slice() {
            b"-E" => {}
            b"-o" => {
                index += 1;
            }
            _ => result.push(preprocessor[index].clone()),
        }
        index += 1;
    }
    for (index, arg) in frontend.iter().enumerate() {
        if arg == b"-emit-pch" {
            result.push(arg.clone());
        } else if (arg == b"-o" || arg == b"--output-pch" || arg == b"--output-pch=")
            && index + 1 < frontend.len()
        {
            result.extend_from_slice(&frontend[index..index + 2]);
        } else if arg.starts_with(b"--output-pch=") {
            result.push(arg.clone());
        }
    }
    result
}

impl Mapper {
    fn correct_cfi(&mut self, path: &Path) -> io::Result<Option<PathBuf>> {
        use std::io::{BufRead, Seek, SeekFrom, Write};
        let Some(strategy) = self.correct_cfi_sections else {
            return Ok(None);
        };
        if !path.starts_with(&self.aux) || path.extension().is_none_or(|extension| extension != "s")
        {
            return Ok(None);
        }
        if let Some(corrected) = self.corrections.get(path) {
            return Ok(Some(corrected.clone()));
        }
        if self.corrections.values().any(|corrected| corrected == path) {
            return Ok(None);
        }
        if matches!(
            strategy,
            CfiCorrection::RiscvAlignment | CfiCorrection::RiscvNoRelax
        ) {
            let corrected = path.with_extension("cfi.s");
            let mut output = fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(&corrected)?;
            // Match the direct frontend's minimum text-section alignment.
            // Scoped norelax keeps this leading directive from adding NOPs.
            output.write_all(if strategy == CfiCorrection::RiscvNoRelax {
                b".text\n.option norelax\n.balign 4\n".as_slice()
            } else {
                b".text\n.option push\n.option norelax\n.balign 4\n.option pop\n".as_slice()
            })?;
            io::copy(&mut fs::File::open(path)?, &mut output)?;
            output.sync_all()?;
            self.corrections
                .insert(path.to_path_buf(), corrected.clone());
            return Ok(Some(corrected));
        }
        let mut reader = io::BufReader::new(fs::File::open(path)?);
        let mut line = Vec::new();
        let mut long = false;
        let mut started = false;
        let mut directive = None;
        let mut scanned = 0u64;
        'scan: loop {
            process::check_cancelled()?;
            let buffer = reader.fill_buf()?;
            if buffer.is_empty() {
                break;
            }
            let length = buffer.len();
            for &byte in buffer {
                scanned += 1;
                if byte == b'\n' {
                    if !long {
                        let value = line.trim_ascii_start();
                        if value.starts_with(b".cfi_sections ")
                            || value.starts_with(b".cfi_sections\t")
                        {
                            if strategy == CfiCorrection::DirectiveOrder && started
                                || strategy == CfiCorrection::Arm45Sections
                                    && value
                                        .split(|byte| byte.is_ascii_whitespace())
                                        .filter(|part| !part.is_empty())
                                        .nth(1)
                                        == Some(b".debug_frame".as_slice())
                            {
                                directive = Some((line.clone(), scanned - line.len() as u64 - 1));
                            }
                            break 'scan;
                        }
                        if value.starts_with(b".cfi_startproc") {
                            started = true;
                        }
                    }
                    line.clear();
                    long = false;
                } else if line.len() < 256 {
                    line.push(byte);
                } else {
                    long = true;
                }
            }
            reader.consume(length);
        }
        let Some((mut directive, position)) = directive else {
            return Ok(None);
        };
        directive.push(b'\n');
        let corrected = path.with_extension("cfi.s");
        let mut output = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(&corrected)?;
        let mut source = fs::File::open(path)?;
        if strategy == CfiCorrection::DirectiveOrder {
            output.write_all(&directive)?;
        } else {
            io::copy(&mut io::Read::take(&mut source, position), &mut output)?;
            output.write_all(b"\t.cfi_sections\t.eh_frame, .debug_frame\n")?;
            source.seek(SeekFrom::Start(position + directive.len() as u64))?;
        }
        io::copy(&mut source, &mut output)?;
        output.sync_all()?;
        self.corrections
            .insert(path.to_path_buf(), corrected.clone());
        Ok(Some(corrected))
    }

    fn rewrite(&mut self, pid: i32, original: &[Vec<u8>]) -> io::Result<Rewritten> {
        let cwd = fs::read_link(format!("/proc/{pid}/cwd"))?;
        let native_command = if self.native_from_ir && original.iter().any(|arg| arg == b"-cc1as") {
            let output = original
                .windows(2)
                .find(|pair| pair[0] == b"-o")
                .map(|pair| pair[1].clone());
            original.iter().find_map(|arg| {
                self.paths
                    .get(&absolute(&cwd, arg))
                    .and_then(|paths| paths.last())
                    .and_then(|saved| {
                        self.native_backends
                            .get(&saved.path)
                            .zip(output.as_ref())
                            .map(|(backend, output)| {
                                let mut args = backend.arguments.clone();
                                for index in 1..args.len() {
                                    if args[index] == b"-S" {
                                        args[index] = b"-emit-obj".to_vec();
                                    }
                                    if args[index - 1] == b"-o" {
                                        args[index] = output.clone();
                                    }
                                    if args[index - 1] == b"-x" {
                                        args[index] = b"ir".to_vec();
                                    }
                                }
                                args[backend.input_index] = compiler_view(&self.view, &saved.path);
                                if original.iter().any(|arg| arg == b"-mrelax-all") {
                                    args.push(b"-mrelax-all".to_vec());
                                }
                                if original.iter().any(|arg| arg == b"--mrelax-relocations")
                                    && !args.iter().any(|arg| arg == b"--mrelax-relocations")
                                {
                                    args.push(b"--mrelax-relocations".to_vec());
                                }
                                if original
                                    .iter()
                                    .any(|arg| arg == b"-massembler-fatal-warnings")
                                {
                                    args.push(b"-massembler-fatal-warnings".to_vec());
                                }
                                for pair in original.windows(2) {
                                    if pair[0] == b"-split-dwarf-output" && !args.contains(&pair[0])
                                    {
                                        args.extend_from_slice(pair);
                                    }
                                }
                                // The actual integrated-assembler job owns this
                                // selection. Old LLVM uses a bare boolean flag;
                                // newer releases use a joined compression kind.
                                args.retain(|arg| {
                                    arg != b"-compress-debug-sections"
                                        && !arg.starts_with(b"-compress-debug-sections=")
                                });
                                for arg in original {
                                    if arg == b"-compress-debug-sections"
                                        || arg.starts_with(b"-compress-debug-sections=")
                                    {
                                        args.push(arg.clone());
                                    }
                                }
                                args
                            })
                    })
            })
        } else {
            None
        };
        let original = native_command.as_deref().unwrap_or(original);
        // Saved cpp-output loses the macro table needed by a PCH consumer.
        // Keep the extra preprocessing as evidence, but serialize the original
        // header with its original preprocessing options. There is one PCH
        // compilation; its macro state is never reconstructed from expanded text.
        let pch_command = if self.redirect
            && self.pch_producer
            && original.iter().any(|arg| {
                arg == b"-emit-pch" || arg == b"--output-pch" || arg.starts_with(b"--output-pch=")
            }) {
            original.iter().find_map(|arg| {
                self.paths
                    .get(&absolute(&cwd, arg))
                    .and_then(|paths| paths.last())
                    .and_then(|output| self.pch_preprocessing.get(&output.path))
                    .map(|preprocessor| pch_frontend(preprocessor, original))
            })
        } else {
            None
        };
        let original = pch_command.as_deref().unwrap_or(original);
        let mut args = original.to_vec();
        let retain_native_backend = self.native_from_ir && original.iter().any(|arg| arg == b"-S");
        if retain_native_backend {
            for arg in &mut args {
                if arg == b"-S" {
                    *arg = b"-emit-llvm-bc".to_vec();
                }
            }
            args.push(b"-disable-llvm-optzns".to_vec());
        }
        let mut source_alias = None;
        let mut source_index = None;
        if self.redirect
            && self.gcc_directives_only
            && original.iter().any(|arg| arg == b"-fpreprocessed")
        {
            let source = original.iter().enumerate().find_map(|(index, arg)| {
                let key = absolute(&cwd, arg);
                self.paths
                    .get(&key)
                    .and_then(|paths| paths.last())
                    .and_then(|output| {
                        self.preprocessing_sources
                            .get(&output.path)
                            .map(|source| (index, output.path.clone(), source.clone()))
                    })
            });
            if let Some((index, input, source)) = source {
                // Keep the original main-file spelling for __BASE_FILE__ and
                // let GCC apply the caller's maps itself. Only this frontend's
                // reads of that name use the saved preprocessing. This also
                // works before macro-prefix-map and for filenames containing '='.
                source_alias = Some(SourceAlias {
                    original: absolute(&cwd, &source),
                    saved: input,
                });
                args[index] = source;
                source_index = Some(index);
            }
        }
        if self.redirect
            && self.normalize_directives_only
            && original.iter().any(|arg| arg == b"-E")
        {
            // In a normal Clang compile this option does not defer macro
            // expansion. Our added saved-preprocessing stage must expand
            // macros once, preserving __COUNTER__ and source-file builtins.
            for arg in &mut args {
                if arg == b"-fdirectives-only" {
                    *arg = b"-E".to_vec();
                }
            }
        }
        let emits_pch = original.iter().any(|argument| argument == b"-emit-pch");
        if emits_pch {
            for argument in &mut args {
                if argument == b"c-header-cpp-output" {
                    *argument = b"cpp-output".to_vec();
                } else if argument == b"c++-header-cpp-output" {
                    *argument = b"c++-cpp-output".to_vec();
                }
            }
        }
        if self.legacy_lto && self.redirect {
            for argument in args.iter_mut().skip(1) {
                for prefix in [
                    b"-plugin-opt=-fresolution=".as_slice(),
                    b"-fresolution=",
                    b"-fltrans-output-list=",
                ] {
                    if let Some(value) = argument.strip_prefix(prefix) {
                        let wanted = absolute(&cwd, value);
                        if !Path::new(OsStr::from_bytes(&wanted)).starts_with(&self.aux) {
                            let partition = prefix == b"-fltrans-output-list=";
                            let path = self.aux.join(if partition {
                                "gcc-lto.ltrans.out"
                            } else {
                                "gcc-resolution.res"
                            });
                            let mapped = Output {
                                path: path.clone(),
                                original: wanted.clone(),
                                role: if partition {
                                    "lto-metadata"
                                } else {
                                    "link-resolution"
                                },
                                staged: true,
                            };
                            self.paths.entry(wanted).or_insert_with(|| vec![mapped]);
                            *argument =
                                [prefix, compiler_view(&self.view, &path).as_slice()].concat();
                        }
                        break;
                    }
                }
            }
        }
        if original.windows(2).any(|pair| {
            pair[0] == b"-x"
                && [b"cpp-output".as_slice(), b"c++-cpp-output"].contains(&pair[1].as_slice())
        }) {
            if let Some(index) = original
                .iter()
                .position(|argument| argument == b"-fmodules")
            {
                if !self.module_maps.is_empty() {
                    // Clang drops include search paths between its saved
                    // preprocessing and IR stages. Retain only module maps
                    // actually opened by that preprocessing process. A response
                    // operand preserves argc and the existing child stack.
                    let mut maps = self.module_maps.iter().collect::<Vec<_>>();
                    maps.sort();
                    let mut response = b"-fmodules\n".to_vec();
                    for path in maps {
                        response.extend_from_slice(b"\"-fmodule-map-file=");
                        for &byte in path.as_os_str().as_bytes() {
                            if byte == b'\\' || byte == b'"' {
                                response.push(b'\\');
                            }
                            response.push(byte);
                        }
                        response.extend_from_slice(b"\"\n");
                    }
                    let name = format!("capture-module-flags-{}.rsp", storage::hash(&response));
                    fs::write(self.aux.join(&name), response)?;
                    args[index] =
                        [b"@".as_slice(), self.view.join(name).as_os_str().as_bytes()].concat();
                }
            }
        }
        let output_index = original
            .windows(2)
            .position(|pair| pair[0] == b"-o")
            .map(|index| index + 1);
        let executable = fs::read_link(format!("/proc/{pid}/exe"))?;
        let name = executable.file_name().unwrap_or_default().as_bytes();
        let linker_name = |name: &[u8]| {
            name == b"collect2"
                || name == b"ld"
                || name.starts_with(b"ld.")
                || name.ends_with(b"-ld")
        };
        let script = original
            .get(1)
            .and_then(|arg| Path::new(OsStr::from_bytes(arg)).file_name())
            .map(OsStr::as_bytes);
        let linking = linker_name(name) || script.is_some_and(linker_name);
        let assembling = name == b"as"
            || name.ends_with(b"-as")
            || original.iter().any(|argument| argument == b"-cc1as");
        let mut occurrences = HashMap::new();
        let mut operand_is_metadata = false;
        for (index, arg) in original.iter().enumerate().skip(1) {
            let skip = operand_is_metadata;
            operand_is_metadata = [
                b"-o".as_slice(),
                b"-main-file-name",
                b"-dumpbase",
                b"-dumpbase-ext",
                b"-dumpdir",
                b"-MT",
                b"-MQ",
                b"-rpath",
                b"-rpath-link",
                b"-soname",
                b"-dynamic-linker",
                b"-plugin",
                b"-m",
                b"-I",
                b"-D",
                b"-U",
            ]
            .contains(&arg.as_slice());
            if skip || Some(index) == output_index || Some(index) == source_index {
                continue;
            }
            let key = absolute(&cwd, arg);
            if let Some(versions) = self.paths.get(&key) {
                let output = if linking {
                    let count = occurrences.entry(key).or_insert(0usize);
                    let value = versions
                        .get(*count)
                        .ok_or_else(|| io::Error::other("ambiguous repeated linker input"))?;
                    *count += 1;
                    value
                } else {
                    versions.last().unwrap()
                };
                args[index] = compiler_view(&self.view, &output.path);
            }
        }
        if assembling && self.correct_cfi_sections.is_some() {
            for argument in args.iter_mut().skip(1) {
                if argument.starts_with(b"-") {
                    continue;
                }
                let path = PathBuf::from(OsStr::from_bytes(&absolute(&cwd, argument)));
                if let Some(corrected) = self.correct_cfi(&path)? {
                    *argument = compiler_view(&self.view, &corrected);
                }
            }
        }
        if assembling && original.iter().any(|arg| arg == b"-cc1as") {
            if let Some(features) = args
                .iter()
                .find_map(|arg| {
                    self.x86_nop_features
                        .get(&PathBuf::from(OsStr::from_bytes(&absolute(&cwd, arg))))
                })
                .cloned()
            {
                for feature in features {
                    args.extend([b"-target-feature".to_vec(), feature]);
                }
            }
        }
        let mut output = None;
        if let Some(index) = output_index {
            let wanted = absolute(&cwd, &original[index]);
            if let Some(&role) = self.primary.get(&wanted) {
                if original[index] != b"/dev/null" && original[index] != b"-" {
                    output = Some(Output {
                        path: PathBuf::from(OsStr::from_bytes(&wanted)),
                        original: wanted,
                        role,
                        staged: false,
                    });
                }
            } else if ![b"-".as_slice(), b"/dev/null"].contains(&original[index].as_slice()) {
                let existing = self
                    .paths
                    .values()
                    .flat_map(|v| v.iter())
                    .find(|value| value.path.as_os_str().as_bytes() == wanted)
                    .cloned();
                let path = Path::new(OsStr::from_bytes(&wanted));
                let ltrans = path.file_name().is_some_and(|name| {
                    name.as_bytes().starts_with(b"lto-ltrans")
                        || name.as_bytes().starts_with(b"gcc-lto.ltrans")
                });
                if path.starts_with(&self.aux) && (existing.is_some() || ltrans) {
                    output = existing.or_else(|| {
                        Some(Output {
                            path: PathBuf::from(OsStr::from_bytes(&wanted)),
                            original: wanted.clone(),
                            role: output_role(Path::new(OsStr::from_bytes(&wanted)), &args),
                            staged: true,
                        })
                    });
                } else {
                    let key = storage::hash(&serde_json::to_vec(
                        &args
                            .iter()
                            .map(|arg| normalized(arg, &self.aux))
                            .collect::<Vec<_>>(),
                    )?);
                    let extension = Path::new(OsStr::from_bytes(&wanted))
                        .extension()
                        .unwrap_or_default();
                    let mut path = self.aux.join(key);
                    path.set_extension(extension);
                    let role = output_role(&path, &args);
                    if !self.redirect {
                        return Ok((
                            args,
                            Some(Output {
                                path: PathBuf::from(OsStr::from_bytes(&wanted)),
                                original: wanted,
                                role,
                                staged: false,
                            }),
                            source_alias,
                        ));
                    }
                    let value = Output {
                        path: path.clone(),
                        original: wanted.clone(),
                        role,
                        staged: true,
                    };
                    args[index] = compiler_view(&self.view, &path);
                    self.paths.entry(wanted).or_default().push(value.clone());
                    output = Some(value);
                }
            }
        }
        if emits_pch
            || original.iter().any(|argument| {
                argument == b"-emit-module-interface"
                    || argument == b"-emit-reduced-module-interface"
            })
        {
            for argument in args.iter().skip(1) {
                let path = PathBuf::from(OsStr::from_bytes(&absolute(&cwd, argument)));
                if path.starts_with(&self.aux)
                    && path.extension().is_some_and(|extension| {
                        extension == "i" || extension == "ii" || extension == "iim"
                    })
                    && path.is_file()
                {
                    fs::File::open(path)?
                        .set_times(fs::FileTimes::new().set_modified(std::time::UNIX_EPOCH))?;
                }
            }
        }
        if self.redirect && self.gcc_directives_only && original.iter().any(|arg| arg == b"-E") {
            if let Some(output) = &output {
                let parsed = crate::arguments::analyze(
                    &original[1..]
                        .iter()
                        .cloned()
                        .map(OsString::from_vec)
                        .collect::<Vec<_>>(),
                    &cwd,
                );
                if let Some(source) = parsed.inputs.last() {
                    self.preprocessing_sources.insert(
                        output.path.clone(),
                        if source == b"-" {
                            b"<stdin>".to_vec()
                        } else {
                            source.clone()
                        },
                    );
                }
            }
        }
        if self.redirect && self.pch_producer && original.iter().any(|arg| arg == b"-E") {
            if let Some(output) = &output {
                self.pch_preprocessing
                    .insert(output.path.clone(), original.to_vec());
            }
        }
        if retain_native_backend {
            if let Some(output) = &output {
                let input_index = original
                    .windows(3)
                    .rposition(|args| args[0] == b"-x")
                    .map(|index| index + 2)
                    .ok_or_else(|| {
                        io::Error::other("native backend has no explicit input language")
                    })?;
                self.native_backends.insert(
                    output.path.clone(),
                    NativeBackend {
                        arguments: original.to_vec(),
                        input_index,
                    },
                );
            }
        }
        if self.x86_tuning
            && original
                .windows(2)
                .any(|args| args[0] == b"-tune-cpu" && args[1] == b"generic")
        {
            if let Some(output) = output.as_ref().filter(|output| output.role == "assembly") {
                // cc1as ignores -tune-cpu. Starting with LLVM 18, generic
                // codegen uses the 15-byte NOP tuning feature, while its saved
                // assembler stage defaults to 10 bytes. Preserve that tuning
                // without changing the selected instruction-set CPU.
                let mut features = vec![b"+fast-15bytenop".to_vec()];
                features.extend(
                    original
                        .windows(2)
                        .filter(|args| {
                            args[0] == b"-target-feature"
                                && args[1].get(1..).is_some_and(|feature| {
                                    [
                                        b"fast-7bytenop".as_slice(),
                                        b"fast-11bytenop",
                                        b"fast-15bytenop",
                                    ]
                                    .contains(&feature)
                                })
                        })
                        .map(|args| args[1].clone()),
                );
                self.x86_nop_features.insert(output.path.clone(), features);
            }
        }
        if !self.lto_inputs.is_empty() {
            if let Some(output) = output
                .as_ref()
                .filter(|output| output.staged && ["ir", "object"].contains(&output.role))
            {
                let path = output.path.as_os_str().as_bytes().to_vec();
                if !self.lto_inputs.contains(&path) {
                    self.lto_inputs.push(path);
                }
            }
        }
        Ok((args, output, source_alias))
    }

    fn unlink(&self, pid: i32) -> io::Result<Option<i64>> {
        #[repr(C)]
        struct Info {
            op: u8,
            pad: [u8; 3],
            arch: u32,
            ip: u64,
            sp: u64,
            data: [u64; 8],
        }
        let mut info: Info = unsafe { std::mem::zeroed() };
        ptrace(
            libc::PTRACE_GET_SYSCALL_INFO,
            pid,
            std::mem::size_of::<Info>(),
            &mut info as *mut _ as usize,
        )?;
        if info.op != 1 {
            return Ok(None);
        }
        let mut regs = registers(pid)?;
        let address = match regs.orig_rax as i64 {
            libc::SYS_unlink => regs.rdi,
            libc::SYS_unlinkat => regs.rsi,
            _ => return Ok(None),
        };
        if regs.orig_rax as i64 == libc::SYS_unlinkat && regs.rdi as i32 != libc::AT_FDCWD {
            return Ok(None);
        }
        let path = bytes(pid, address, 4096)?;
        let cwd = fs::read_link(format!("/proc/{pid}/cwd"))?;
        let key = absolute(&cwd, &path);
        if let Some(output) = self.paths.get(&key).and_then(|versions| versions.last()) {
            // The compiler-owned file is staged, and the original path may be
            // a package input. Prevent cleanup from touching that original.
            // Return the status that unlink of the staged file would have had;
            // staging cleanup happens after the driver finishes.
            let result = if output.path.exists() {
                0
            } else {
                -(libc::ENOENT as i64)
            };
            let _ = output;
            regs.orig_rax = libc::SYS_getpid as u64;
            set_registers(pid, &regs)?;
            return Ok(Some(result));
        }
        Ok(None)
    }

    fn linker_file(
        &mut self,
        pid: i32,
        memory: u64,
        source_alias: Option<&SourceAlias>,
    ) -> io::Result<()> {
        let mut regs = registers(pid)?;
        let address = match regs.orig_rax as i64 {
            libc::SYS_open
            | libc::SYS_creat
            | libc::SYS_unlink
            | libc::SYS_access
            | libc::SYS_stat
            | libc::SYS_lstat => regs.rdi,
            libc::SYS_openat | libc::SYS_unlinkat | libc::SYS_newfstatat | libc::SYS_faccessat
                if regs.rdi as i32 == libc::AT_FDCWD =>
            {
                regs.rsi
            }
            _ => return Ok(()),
        };
        let path = bytes(pid, address, 4096)?;
        let cwd = fs::read_link(format!("/proc/{pid}/cwd"))?;
        let wanted = absolute(&cwd, &path);
        if let Some(alias) = source_alias.filter(|alias| alias.original == wanted) {
            let read_only = match regs.orig_rax as i64 {
                libc::SYS_open => {
                    regs.rsi as i32 & (libc::O_ACCMODE | libc::O_CREAT | libc::O_TRUNC)
                        == libc::O_RDONLY
                }
                libc::SYS_openat => {
                    regs.rdx as i32 & (libc::O_ACCMODE | libc::O_CREAT | libc::O_TRUNC)
                        == libc::O_RDONLY
                }
                libc::SYS_access
                | libc::SYS_stat
                | libc::SYS_lstat
                | libc::SYS_newfstatat
                | libc::SYS_faccessat => true,
                _ => false,
            };
            if read_only {
                let mut data = compiler_view(&self.view, &alias.saved);
                data.push(0);
                if data.len() > 4096 {
                    return Err(io::Error::other(
                        "preprocessed source alias exceeds its limit",
                    ));
                }
                write_bytes(pid, memory, &data)?;
                match regs.orig_rax as i64 {
                    libc::SYS_openat | libc::SYS_newfstatat | libc::SYS_faccessat => {
                        regs.rsi = memory
                    }
                    _ => regs.rdi = memory,
                }
                set_registers(pid, &regs)?;
                return Ok(());
            }
        }
        let requested = Path::new(OsStr::from_bytes(&wanted));
        if requested.starts_with(&self.aux) && !requested.exists() {
            if let Some(source) = requested
                .file_name()
                .and_then(|name| self.pch_sources.get(name.as_bytes()))
            {
                let writable = match regs.orig_rax as i64 {
                    libc::SYS_open => {
                        regs.rsi as i32
                            & (libc::O_WRONLY | libc::O_RDWR | libc::O_CREAT | libc::O_TRUNC)
                            != 0
                    }
                    libc::SYS_openat => {
                        regs.rdx as i32
                            & (libc::O_WRONLY | libc::O_RDWR | libc::O_CREAT | libc::O_TRUNC)
                            != 0
                    }
                    libc::SYS_creat | libc::SYS_unlink | libc::SYS_unlinkat => true,
                    _ => false,
                };
                if !writable {
                    let mut data = source.as_os_str().as_bytes().to_vec();
                    data.push(0);
                    if data.len() > 4096 {
                        return Err(io::Error::other("PCH source path exceeds its limit"));
                    }
                    write_bytes(pid, memory, &data)?;
                    match regs.orig_rax as i64 {
                        libc::SYS_openat | libc::SYS_newfstatat | libc::SYS_faccessat => {
                            regs.rsi = memory
                        }
                        _ => regs.rdi = memory,
                    }
                    set_registers(pid, &regs)?;
                    return Ok(());
                }
            }
        }
        if self.redirect_debug_objects && wanted.ends_with(b".debug.temp.o") {
            let flags = match regs.orig_rax as i64 {
                libc::SYS_open => Some(regs.rsi),
                libc::SYS_openat => Some(regs.rdx),
                libc::SYS_creat => Some((libc::O_CREAT | libc::O_WRONLY) as u64),
                _ => None,
            };
            if flags.is_some_and(|flags| flags as i32 & (libc::O_CREAT | libc::O_TRUNC) != 0) {
                let name = format!("gcc-lto-debug-{}.o", storage::hash(&wanted));
                self.paths.entry(wanted.clone()).or_insert_with(|| {
                    vec![Output {
                        path: self.aux.join(name),
                        original: wanted.clone(),
                        role: "object",
                        staged: true,
                    }]
                });
            }
        }
        if self.legacy_lto || self.redirect_debug_objects {
            if let Some(output) = self.paths.get(&wanted).and_then(|values| values.last()) {
                let mut data = compiler_view(&self.view, &output.path);
                data.push(0);
                if data.len() > 4096 {
                    return Err(io::Error::other("mapped LTO path exceeds 4096 bytes"));
                }
                write_bytes(pid, memory, &data)?;
                match regs.orig_rax as i64 {
                    libc::SYS_openat
                    | libc::SYS_unlinkat
                    | libc::SYS_newfstatat
                    | libc::SYS_faccessat => regs.rsi = memory,
                    _ => regs.rdi = memory,
                }
                set_registers(pid, &regs)?;
                return Ok(());
            }
        }
        for primary in self
            .primary
            .iter()
            .filter_map(|(path, role)| ["linked-output", "object"].contains(role).then_some(path))
            .chain(self.lto_inputs.iter())
        {
            let Some(suffix) = wanted.strip_prefix(primary.as_slice()) else {
                continue;
            };
            // LLVMgold's measured save-temps outputs. The requested ELF path
            // itself is never redirected, nor are arbitrary linker operands.
            if !llvm_lto_suffix(suffix) {
                continue;
            }
            let flags = match regs.orig_rax as i64 {
                libc::SYS_open => Some(regs.rsi),
                libc::SYS_openat => Some(regs.rdx),
                libc::SYS_creat => Some((libc::O_CREAT | libc::O_WRONLY) as u64),
                _ => None,
            };
            if flags.is_some_and(|flags| {
                flags as i32 & (libc::O_CREAT | libc::O_TRUNC | libc::O_WRONLY | libc::O_RDWR) != 0
            }) {
                self.linker_generated.insert(wanted.clone());
            }
            if !self.linker_generated.contains(&wanted) {
                continue;
            }
            let input_prefix = format!(
                "llvm-lto-input-{}",
                storage::hash(normalized(primary, &self.aux).as_bytes())
            );
            let prefix = if self.lto_inputs.contains(primary) {
                input_prefix.as_bytes()
            } else if suffix == b".lto_wrapper_args" {
                b"gcc-lto".as_slice()
            } else {
                b"llvm-lto".as_slice()
            };
            let name = [prefix, suffix].concat();
            if suffix == b".lto_wrapper_args" {
                self.paths.entry(wanted.clone()).or_insert_with(|| {
                    vec![Output {
                        path: self.aux.join(OsStr::from_bytes(&name)),
                        original: wanted.clone(),
                        role: "response",
                        staged: true,
                    }]
                });
            }
            let destination = self.view.join(OsStr::from_bytes(&name));
            let mut data = destination.as_os_str().as_bytes().to_vec();
            data.push(0);
            if data.len() > 4096 {
                return Err(io::Error::other("linker auxiliary path exceeds 4096 bytes"));
            }
            write_bytes(pid, memory, &data)?;
            match regs.orig_rax as i64 {
                libc::SYS_openat
                | libc::SYS_unlinkat
                | libc::SYS_newfstatat
                | libc::SYS_faccessat => regs.rsi = memory,
                _ => regs.rdi = memory,
            }
            set_registers(pid, &regs)?;
            break;
        }
        Ok(())
    }
}

pub fn run(
    executable: &Path,
    args: &[OsString],
    invocation: &mut Invocation,
    options: Options<'_>,
) -> io::Result<Outcome> {
    let stdout_role = options.stdout_role;
    process::install_handlers()?;
    let mut command = Command::new(executable);
    command.args(args);
    command.env("DATASET_CAPTURE_TRACER", std::process::id().to_string());
    unsafe {
        command.pre_exec(|| {
            crate::signals::restore_sigpipe();
            if libc::setpgid(0, 0) != 0 {
                return Err(io::Error::last_os_error());
            }
            if libc::ptrace(libc::PTRACE_TRACEME, 0, 0, 0) < 0 {
                return Err(io::Error::last_os_error());
            }
            Ok(())
        });
    }
    let (stream, stream_error) = if stdout_role.is_some() {
        match invocation.start_stream() {
            Ok(stream) => {
                command.stdout(std::process::Stdio::piped());
                (Some(stream), None)
            }
            Err(error) => (None, Some(error.to_string())),
        }
    } else {
        (None, None)
    };
    let mut child = command.spawn()?;
    let root = child.id() as i32;
    process::register_child(root);
    let shared = Mutex::new(invocation);
    std::thread::scope(|scope| {
        let pump = stream.map(|mut stream| {
            let source = child.stdout.take().unwrap();
            let shared = &shared;
            scope.spawn(move || {
                let result = process::pump_stdout(source, root, |data| {
                    shared.lock().unwrap().stream_chunk(&mut stream, data)
                });
                (stream, result)
            })
        });
        let result = run_started(root, &shared, options);
        let pumped = pump
            .map(|pump| {
                pump.join()
                    .map_err(|_| io::Error::other("traced stdout pump failed"))
            })
            .transpose()?;
        let mut outcome = result?;
        if let Some(error) = stream_error {
            outcome
                .errors
                .push(format!("stdout initialization: {error}"));
        }
        if let Some((stream, result)) = pumped {
            let error = match result {
                Ok(error) => error,
                Err(error) => Some(error.to_string()),
            };
            if let Some(error) = error {
                let _ = shared.lock().unwrap().discard_stream(stream);
                outcome.errors.push(format!("stdout capture: {error}"));
            } else {
                match shared.lock().unwrap().finish_stream(stream) {
                    Ok(mut artifact) => {
                        artifact.role = stdout_role.unwrap().into();
                        outcome.artifacts.push(artifact);
                    }
                    Err(error) => outcome.errors.push(format!("stdout snapshot: {error}")),
                }
            }
        }
        Ok(outcome)
    })
}

fn run_started(
    root: i32,
    invocation: &Mutex<&mut Invocation>,
    options: Options<'_>,
) -> io::Result<Outcome> {
    let Options {
        primaries,
        nix_response_default,
        primary_role,
        redirect,
        view,
        correct_cfi_sections,
        legacy_lto,
        redirect_debug_objects,
        lto_inputs,
        pch_sources,
        pch_producer,
        root_linker,
        split_dwarf,
        normalize_directives_only,
        gcc_directives_only,
        x86_tuning,
        native_from_ir,
        ..
    } = options;
    let mut guard = Cleanup {
        root,
        live: HashSet::from([root]),
        armed: true,
    };
    let (_, initial) = wait(root)?;
    if !libc::WIFSTOPPED(initial) || libc::WSTOPSIG(initial) != libc::SIGTRAP {
        return Err(io::Error::other(
            "compiler did not stop at its initial exec",
        ));
    }
    ptrace(
        libc::PTRACE_SETOPTIONS,
        root,
        0,
        (libc::PTRACE_O_EXITKILL
            | libc::PTRACE_O_TRACEEXEC
            | libc::PTRACE_O_TRACEFORK
            | libc::PTRACE_O_TRACEVFORK
            | libc::PTRACE_O_TRACECLONE
            | libc::PTRACE_O_TRACEEXIT
            | libc::PTRACE_O_TRACESYSGOOD) as usize,
    )?;
    let cwd = std::env::current_dir()?;
    let staging_root = invocation.lock().unwrap().root.clone();
    let mut header_inputs = std::collections::BTreeMap::new();
    let object_root = invocation.lock().unwrap().root.join("objects");
    let mut mapper = Mapper {
        paths: HashMap::new(),
        primary: primaries
            .iter()
            .map(|p| (absolute(&cwd, p.as_os_str().as_bytes()), primary_role))
            .collect(),
        aux: invocation.lock().unwrap().aux.clone(),
        view: view.to_path_buf(),
        redirect,
        module_maps: HashSet::new(),
        correct_cfi_sections,
        corrections: HashMap::new(),
        legacy_lto,
        lto_started: false,
        linker_generated: HashSet::new(),
        redirect_debug_objects,
        lto_inputs: lto_inputs
            .iter()
            .map(|input| absolute(&cwd, input))
            .collect(),
        pch_sources: pch_sources
            .iter()
            .map(|artifact| {
                let name = crate::pch::name(artifact)?;
                if name.contains(&b'/') || name == b"." || name == b".." {
                    return Err(io::Error::other("invalid saved PCH source basename"));
                }
                Ok((name, object_root.join(&artifact.sha256)))
            })
            .collect::<io::Result<HashMap<_, _>>>()?,
        normalize_directives_only,
        gcc_directives_only,
        preprocessing_sources: HashMap::new(),
        pch_preprocessing: HashMap::new(),
        pch_producer,
        x86_tuning,
        x86_nop_features: HashMap::new(),
        native_from_ir,
        native_backends: HashMap::new(),
    };
    let mut tasks = HashMap::from([(
        root,
        Task {
            driver: true,
            ..Task::default()
        },
    )]);
    let mut overridden_syscalls = HashMap::<i32, i64>::new();
    let mut path_pools = HashMap::<u64, usize>::new();
    let mut unclaimed_stops = HashSet::new();
    let mut artifacts = Vec::new();
    let mut subprocesses = Vec::new();
    let mut errors = Vec::new();
    let mut root_status = None;
    ptrace(libc::PTRACE_SYSCALL, root, 0, 0)?;
    if root_linker {
        // A direct ELF linker has no later exec at which to install path
        // storage. Allocate it at a completed syscall before any LTO work.
        let (_, status) = wait(root)?;
        if !libc::WIFSTOPPED(status) || libc::WSTOPSIG(status) != (libc::SIGTRAP | 0x80) {
            return Err(io::Error::other(
                "linker did not reach its initial syscall stop",
            ));
        }
        #[repr(C)]
        struct Info {
            op: u8,
            pad: [u8; 3],
            arch: u32,
            ip: u64,
            sp: u64,
            data: [u64; 8],
        }
        let mut info: Info = unsafe { std::mem::zeroed() };
        ptrace(
            libc::PTRACE_GET_SYSCALL_INFO,
            root,
            std::mem::size_of::<Info>(),
            &mut info as *mut _ as usize,
        )?;
        if info.op == 1 {
            ptrace(libc::PTRACE_SYSCALL, root, 0, 0)?;
            let (_, status) = wait(root)?;
            if !libc::WIFSTOPPED(status) || libc::WSTOPSIG(status) != (libc::SIGTRAP | 0x80) {
                return Err(io::Error::other(
                    "linker did not complete its initial syscall",
                ));
            }
        } else if info.op != 2 {
            return Err(io::Error::other("unknown initial linker syscall stop"));
        }
        let pool = remote_memory(root, 1024 * 4096)?;
        let task = tasks.get_mut(&root).unwrap();
        task.path_pool = Some(pool);
        task.path_memory = Some(pool);
        path_pools.insert(pool, 1);
        ptrace(libc::PTRACE_SYSCALL, root, 0, 0)?;
    }
    while !guard.live.is_empty() {
        let (pid, status) = wait(-1)?;
        if libc::WIFEXITED(status) || libc::WIFSIGNALED(status) {
            guard.live.remove(&pid);
            tasks.remove(&pid);
            overridden_syscalls.remove(&pid);
            if pid == root {
                root_status = Some(ExitStatus::from_raw(status));
                if !guard.live.is_empty() {
                    errors.push("compiler left background subprocesses".to_string());
                    for &other in &guard.live {
                        unsafe {
                            libc::kill(other, libc::SIGKILL);
                        }
                    }
                }
            }
            continue;
        }
        if !libc::WIFSTOPPED(status) {
            continue;
        }
        let signal = libc::WSTOPSIG(status);
        let event = status >> 16;
        // Linux may report a new child's automatic stop before its parent's
        // clone event. Keep it stopped until that event assigns its tracing
        // state and private path buffer. Resuming it here can miss file writes.
        if signal == libc::SIGSTOP && event == 0 && !guard.live.contains(&pid) {
            unclaimed_stops.insert(pid);
            continue;
        }
        let mut deliver = 0;
        match event {
            libc::PTRACE_EVENT_FORK | libc::PTRACE_EVENT_VFORK | libc::PTRACE_EVENT_CLONE => {
                let mut new_pid = 0u64;
                ptrace(
                    libc::PTRACE_GETEVENTMSG,
                    pid,
                    0,
                    &mut new_pid as *mut _ as usize,
                )?;
                guard.live.insert(new_pid as i32);
                let parent = tasks.get(&pid);
                let mut task = Task {
                    driver: parent.is_some_and(|task| task.driver),
                    modules: parent.is_some_and(|task| task.modules),
                    headers: parent.is_some_and(|task| task.headers),
                    source_alias: parent.and_then(|task| task.source_alias.clone()),
                    ..Task::default()
                };
                if let Some(pool) = parent.and_then(|task| task.path_pool) {
                    let next = path_pools.get_mut(&pool).unwrap();
                    if *next >= 1024 {
                        return Err(io::Error::other(
                            "too many linker threads for auxiliary path storage",
                        ));
                    }
                    task.path_pool = Some(pool);
                    task.path_memory = Some(pool + (*next * 4096) as u64);
                    *next += 1;
                }
                let driver = task.driver;
                tasks.insert(new_pid as i32, task);
                if unclaimed_stops.remove(&(new_pid as i32)) {
                    ptrace(
                        if driver {
                            libc::PTRACE_SYSCALL
                        } else {
                            libc::PTRACE_CONT
                        },
                        new_pid as i32,
                        0,
                        0,
                    )?;
                }
            }
            libc::PTRACE_EVENT_EXEC => {
                // Finish execve's syscall-exit stop before injecting mmap.
                // Otherwise the kernel overwrites the injected syscall number
                // with execve's successful return value on the next resume.
                ptrace(libc::PTRACE_SYSCALL, pid, 0, 0)?;
                let (_, exit_stop) = wait(pid)?;
                if !libc::WIFSTOPPED(exit_stop)
                    || libc::WSTOPSIG(exit_stop) != (libc::SIGTRAP | 0x80)
                {
                    return Err(io::Error::other(
                        "execve did not reach its syscall-exit stop",
                    ));
                }
                let (registers, original) = arguments(pid)?;
                let program = fs::read_link(format!("/proc/{pid}/exe"))?;
                let (rewritten, output, source_alias) = mapper.rewrite(pid, &original)?;
                for argument in &rewritten {
                    if let Some(path) = argument.strip_prefix(b"@") {
                        let path = Path::new(OsStr::from_bytes(path));
                        if path.starts_with(&mapper.view)
                            && path.file_name().is_some_and(|name| {
                                name.as_bytes().starts_with(b"capture-module-flags-")
                                    || name.as_bytes().starts_with(b"capture-gcc-macro-flags-")
                            })
                        {
                            let name = path.file_name().unwrap();
                            match invocation.lock().unwrap().snapshot(
                                &mapper.aux.join(name),
                                "response",
                                "invocation-stage",
                                name.as_bytes(),
                            ) {
                                Ok(artifact) => artifacts.push(artifact),
                                Err(error) => {
                                    errors.push(format!("module flags snapshot: {error}"))
                                }
                            }
                        }
                    }
                }
                let stack = replace_arguments(pid, registers.rsp, &original, &rewritten)?;
                let task = tasks.entry(pid).or_default();
                task.source_alias = source_alias;
                task.output = output.or(task.output.take());
                let filename = program.file_name().unwrap_or_default().as_bytes();
                if filename == b"lto-wrapper" {
                    mapper.lto_started = true;
                }
                let environment = if legacy_lto && filename == b"lto-wrapper" {
                    gcc_auxiliary_environment(
                        pid,
                        stack,
                        rewritten.len(),
                        Some(&mapper.view.join("gcc-lto")),
                    )?
                } else if filename == b"collect2" {
                    gcc_auxiliary_environment(pid, stack, rewritten.len(), None)?
                } else {
                    None
                };
                let linker = filename == b"ld"
                    || filename == b"lld"
                    || filename.ends_with(b"-ld")
                    || filename.ends_with(b"ld.bfd")
                    || filename.ends_with(b"ld.gold")
                    || filename.ends_with(b"ld.lld");
                let coordinator = filename == b"lto-wrapper"
                    || filename == b"lto1"
                    || filename == b"gcc"
                    || filename.ends_with(b"-gcc");
                task.path_memory = if task.source_alias.is_some()
                    || !mapper.pch_sources.is_empty()
                    || (legacy_lto || redirect_debug_objects) && (linker || coordinator)
                    || linker
                        && original
                            .iter()
                            .any(|argument| argument == b"-plugin-opt=save-temps")
                {
                    Some(remote_memory(pid, 1024 * 4096)?)
                } else {
                    None
                };
                task.path_pool = task.path_memory;
                if let Some(pool) = task.path_pool {
                    path_pools.insert(pool, 1);
                }
                task.driver = pid == root
                    || (filename.starts_with(b"clang")
                        || filename == b"gcc"
                        || filename == b"g++"
                        || filename.ends_with(b"-gcc")
                        || filename.ends_with(b"-g++"))
                        && !original
                            .iter()
                            .any(|arg| arg == b"-cc1" || arg == b"-cc1as");
                task.driver |= task.path_memory.is_some();
                task.driver |= legacy_lto && filename == b"collect2";
                task.lto_wpa = filename == b"lto1"
                    && original
                        .iter()
                        .any(|argument| argument.starts_with(b"-fwpa"));
                task.modules = split_dwarf
                    || !mapper.pch_sources.is_empty()
                    || original.iter().any(|argument| {
                        argument == b"-fmodules"
                            || argument == b"-fmodules-ts"
                            || argument == b"-emit-module-interface"
                            || argument == b"-emit-reduced-module-interface"
                            || argument == b"-include-pch"
                            || argument.starts_with(b"-fmodule-file=")
                    });
                task.driver |= task.modules;
                task.headers = pch_producer && original.iter().any(|argument| argument == b"-E");
                task.driver |= task.headers;
                let compiler_command = task.output.is_some()
                    || task.driver
                    || filename == b"cc1"
                    || filename == b"cc1plus"
                    || filename == b"as"
                    || filename.ends_with(b"-as")
                    || filename == b"lto1"
                    || filename == b"lto-wrapper";
                if !compiler_command {
                    ptrace(libc::PTRACE_CONT, pid, 0, 0)?;
                    continue;
                }
                let metadata_too_large = original
                    .iter()
                    .chain(&rewritten)
                    .map(|a| a.len().saturating_mul(2) + 8)
                    .sum::<usize>()
                    > 3 * 1024 * 1024;
                if task.execs.len() >= 16
                    || metadata_too_large
                    || subprocesses.len() >= MAX_COMMANDS
                {
                    let error = "subprocess metadata exceeds the capture limit".to_string();
                    if !errors.contains(&error) {
                        errors.push(error);
                    }
                    let driver = task.driver;
                    ptrace(
                        if driver {
                            libc::PTRACE_SYSCALL
                        } else {
                            libc::PTRACE_CONT
                        },
                        pid,
                        0,
                        0,
                    )?;
                    continue;
                }
                let mut recorded = original
                    .iter()
                    .map(|a| normalized(a, &mapper.aux))
                    .collect::<Vec<_>>();
                let mut effective = rewritten
                    .iter()
                    .map(|a| normalized(a, &mapper.aux))
                    .collect::<Vec<_>>();
                let response_args = original
                    .iter()
                    .skip(1)
                    .map(|arg| OsString::from_vec(arg.clone()))
                    .collect::<Vec<_>>();
                if let Some(path) =
                    crate::arguments::nix_response_file(&response_args, nix_response_default)
                {
                    match invocation.lock().unwrap().snapshot(
                        &path,
                        "response",
                        "wrapper-response",
                        b"cc-params.rsp",
                    ) {
                        Ok(artifact) => {
                            recorded[1] = storage::hex(b"@${NIX_RESPONSE_FILE}");
                            effective[1] = recorded[1].clone();
                            artifacts.push(artifact);
                        }
                        Err(err) => errors.push(format!("subprocess response snapshot: {err}")),
                    }
                }
                if legacy_lto && mapper.lto_started && task.driver && response_args.len() == 1 {
                    if let Some(name) = response_args[0].as_bytes().strip_prefix(b"@") {
                        let path = Path::new(OsStr::from_bytes(name));
                        let temporary = std::env::var_os("TMPDIR")
                            .map(PathBuf::from)
                            .unwrap_or_else(|| PathBuf::from("/tmp"));
                        if path.parent() == Some(temporary.as_path())
                            && path
                                .file_name()
                                .is_some_and(|name| name.as_bytes().starts_with(b"cc"))
                        {
                            match invocation.lock().unwrap().snapshot(
                                path,
                                "response",
                                "compiler-response",
                                b"gcc-params.rsp",
                            ) {
                                Ok(artifact) => {
                                    recorded[1] = storage::hex(b"@${GCC_RESPONSE_FILE}");
                                    effective[1] = recorded[1].clone();
                                    artifacts.push(artifact);
                                }
                                Err(error) => {
                                    errors.push(format!("GCC response snapshot: {error}"))
                                }
                            }
                        }
                    }
                }
                task.execs.push(
                    json!({"executable_hex":storage::hex(program.as_os_str().as_bytes()),
                    "arguments_hex":recorded,"effective_arguments_hex":effective,"environment_override":environment }),
                );
            }
            libc::PTRACE_EVENT_EXIT => {
                let task = tasks.entry(pid).or_default();
                if !task.execs.is_empty() || !task.module_paths.is_empty() {
                    let mut exit = 0u64;
                    ptrace(
                        libc::PTRACE_GETEVENTMSG,
                        pid,
                        0,
                        &mut exit as *mut _ as usize,
                    )?;
                    let status = ExitStatus::from_raw(exit as i32);
                    let mut output_ids = Vec::new();
                    if task.lto_wpa {
                        for entry in fs::read_dir(&mapper.aux)?.take(257) {
                            let entry = entry?;
                            let name = entry.file_name();
                            let bytes = name.as_bytes();
                            if !bytes.ends_with(b".o")
                                || !(bytes.starts_with(b"gcc-lto")
                                    || bytes.starts_with(b"lto-ltrans"))
                            {
                                continue;
                            }
                            match invocation.lock().unwrap().snapshot(
                                &entry.path(),
                                "ir",
                                "invocation-stage",
                                bytes,
                            ) {
                                Ok(artifact) => {
                                    output_ids.push(artifact.sha256.clone());
                                    artifacts.push(artifact);
                                }
                                Err(error) => {
                                    errors.push(format!("WPA partition snapshot: {error}"))
                                }
                            }
                        }
                    }
                    if let Some(output) = &task.output {
                        if status.success() && output.path.is_file() {
                            let relative = if output.staged {
                                output
                                    .path
                                    .strip_prefix(&mapper.aux)
                                    .unwrap()
                                    .as_os_str()
                                    .as_bytes()
                            } else {
                                &output.original
                            };
                            match invocation.lock().unwrap().snapshot(
                                &output.path,
                                output.role,
                                if output.staged {
                                    "invocation-stage"
                                } else {
                                    "cwd"
                                },
                                relative,
                            ) {
                                Ok(artifact) => {
                                    output_ids.push(artifact.sha256.clone());
                                    if artifact.role == "preprocessing"
                                        && output.path.extension().is_some_and(|ext| ext == "s")
                                    {
                                        let mut assembly = artifact.clone();
                                        assembly.role = "assembly".into();
                                        artifacts.push(assembly);
                                    }
                                    artifacts.push(artifact);
                                }
                                Err(error) => errors.push(format!("subprocess snapshot: {error}")),
                            }
                        } else if status.success() {
                            errors.push(
                                "compiler subprocess output is missing or is not regular".into(),
                            );
                        }
                    }
                    for path in &task.module_paths {
                        if !path.is_file() {
                            continue;
                        }
                        // Saved Clang stages can publish an empty placeholder
                        // before the assembler writes the actual split DWARF.
                        if path.extension().is_some_and(|extension| extension == "dwo")
                            && path.metadata()?.len() == 0
                        {
                            continue;
                        }
                        let role = match path.extension().map(OsStr::as_bytes) {
                            Some(b"pcm" | b"gcm") => "module",
                            Some(b"modulemap") => "module-map",
                            Some(b"dwo") => "debug-companion",
                            _ => "pch",
                        };
                        let staged = path.strip_prefix(&mapper.aux).ok();
                        match invocation.lock().unwrap().snapshot(
                            path,
                            role,
                            if staged.is_some() {
                                "invocation-stage"
                            } else {
                                "cwd"
                            },
                            staged.unwrap_or(path).as_os_str().as_bytes(),
                        ) {
                            Ok(artifact) => {
                                output_ids.push(artifact.sha256.clone());
                                artifacts.push(artifact);
                            }
                            Err(error) => errors.push(format!("module snapshot: {error}")),
                        }
                    }
                    let metadata = json!({"schema":1,"execs":task.execs,"inherited_exec":task.execs.is_empty(),"compiler_status":{"exit_code":status.code(),"signal":status.signal()}});
                    match if subprocesses.len() < MAX_COMMANDS {
                        invocation.lock().unwrap().metadata(&metadata)
                    } else {
                        Err(io::Error::other("subprocess metadata limit exceeded"))
                    } {
                        Ok(artifact) => {
                            subprocesses.push(
                                json!({"command_sha256":artifact.sha256,"outputs":output_ids}),
                            );
                            artifacts.push(artifact);
                        }
                        Err(error) => errors.push(format!("subprocess metadata: {error}")),
                    }
                }
            }
            0 if signal == (libc::SIGTRAP | 0x80) => {
                let observed = (|| -> io::Result<()> {
                    if let Some(value) = overridden_syscalls.remove(&pid) {
                        let mut regs = registers(pid)?;
                        regs.rax = value as u64;
                        set_registers(pid, &regs)?;
                    } else {
                        // rax is -ENOSYS only at syscall entry. The replacement
                        // path lives in memory reserved at exec, so the pending
                        // syscall and the linker's stack remain intact.
                        if registers(pid)?.rax as i64 == -(libc::ENOSYS as i64) {
                            if let Some(memory) = tasks.get(&pid).and_then(|task| task.path_memory)
                            {
                                mapper.linker_file(
                                    pid,
                                    memory,
                                    tasks.get(&pid).and_then(|task| task.source_alias.as_ref()),
                                )?;
                            }
                            if tasks.get(&pid).is_some_and(|task| task.headers) {
                                if let Some(state) = header_input(pid, &staging_root)? {
                                    if header_inputs.len() >= 4096
                                        && !header_inputs.contains_key(&state.path_hex)
                                    {
                                        return Err(io::Error::other(
                                            "too many PCH input dependencies",
                                        ));
                                    }
                                    header_inputs.entry(state.path_hex.clone()).or_insert(state);
                                }
                            }
                            if tasks.get(&pid).is_some_and(|task| task.modules) {
                                if let Some(path) = module_path(pid)? {
                                    if path
                                        .extension()
                                        .is_some_and(|extension| extension == "modulemap")
                                        && path.is_file()
                                        && mapper.module_maps.len() < 256
                                    {
                                        mapper.module_maps.insert(path.clone());
                                    }
                                    let paths = &mut tasks.get_mut(&pid).unwrap().module_paths;
                                    if paths.len() < 256 {
                                        paths.insert(path);
                                    } else if !errors
                                        .iter()
                                        .any(|error| error == "module file limit exceeded")
                                    {
                                        errors.push("module file limit exceeded".into());
                                    }
                                }
                            }
                        }
                        if let Some(value) = mapper.unlink(pid)? {
                            overridden_syscalls.insert(pid, value);
                        }
                    }
                    Ok(())
                })();
                if let Err(error) = observed {
                    // exit_group can retire a stopped LTO worker while its
                    // syscall stop is queued. Reap its exit in the main loop.
                    if error.raw_os_error() == Some(libc::ESRCH) {
                        continue;
                    }
                    return Err(error);
                }
            }
            0 if signal == libc::SIGSTOP => (), // automatic stop of a new tracee
            0 => deliver = signal,
            _ => (),
        }
        let driver = tasks.get(&pid).is_some_and(|task| task.driver);
        let resumed = ptrace(
            if driver {
                libc::PTRACE_SYSCALL
            } else {
                libc::PTRACE_CONT
            },
            pid,
            0,
            deliver as usize,
        );
        if let Err(error) = resumed {
            if error.raw_os_error() != Some(libc::ESRCH) {
                return Err(error);
            }
        }
    }
    guard.armed = false;
    process::child_finished();
    let mut transformations = Vec::new();
    let mut corrections = mapper.corrections.into_iter().collect::<Vec<_>>();
    corrections.sort();
    for (source, corrected) in corrections {
        let mut pair = Vec::new();
        for path in [&source, &corrected] {
            match invocation.lock().unwrap().snapshot(
                path,
                "assembly",
                "invocation-stage",
                path.file_name().unwrap().as_bytes(),
            ) {
                Ok(artifact) => {
                    pair.push(artifact.sha256.clone());
                    artifacts.push(artifact);
                }
                Err(error) => errors.push(format!("CFI assembly snapshot: {error}")),
            }
        }
        if pair.len() == 2 {
            transformations.push(json!({"kind":mapper.correct_cfi_sections.unwrap().kind(),"emitted_assembly_sha256":pair[0],"assembler_input_sha256":pair[1]}));
        }
    }
    let status = root_status.ok_or_else(|| io::Error::other("missing driver wait status"))?;
    subprocesses.sort_by_key(|value| {
        value["command_sha256"]
            .as_str()
            .unwrap_or_default()
            .to_owned()
    });
    Ok(Outcome {
        status,
        lto_started: mapper.lto_started,
        pch_inputs: header_inputs.into_values().collect(),
        artifacts,
        subprocesses,
        errors,
        transformations,
    })
}

struct Cleanup {
    root: i32,
    live: HashSet<i32>,
    armed: bool,
}
impl Drop for Cleanup {
    fn drop(&mut self) {
        if !self.armed {
            return;
        }
        unsafe {
            libc::kill(-self.root, libc::SIGKILL);
        }
        for &pid in &self.live {
            unsafe {
                libc::kill(pid, libc::SIGKILL);
            }
            let _ = ptrace(libc::PTRACE_CONT, pid, 0, libc::SIGKILL as usize);
        }
        while let Ok((pid, status)) = wait(-1) {
            if pid == 0 {
                break;
            }
            if libc::WIFSTOPPED(status) {
                unsafe {
                    libc::kill(pid, libc::SIGKILL);
                }
                let _ = ptrace(libc::PTRACE_CONT, pid, 0, libc::SIGKILL as usize);
            }
        }
        process::child_finished();
    }
}

#[cfg(test)]
mod tests {
    use super::llvm_lto_suffix;

    #[test]
    fn retains_thin_lto_inputs_and_archive_members() {
        for suffix in [
            b".thinlto.o".as_slice(),
            b".opt.bc",
            b".llvm.128.member.c.thinlto.o",
            b".llvm.128.member.c.opt.bc",
            b".llvm.128..thinlto.o",
        ] {
            assert!(llvm_lto_suffix(suffix));
        }
        for suffix in [
            b".llvm.bad.member.c.thinlto.o".as_slice(),
            b".llvm..member.c.thinlto.o",
            b".llvm.128.directory/member.c.thinlto.o",
            b".llvm.128.member.c.other",
            b".thinlto.other",
        ] {
            assert!(!llvm_lto_suffix(suffix));
        }
    }
}
