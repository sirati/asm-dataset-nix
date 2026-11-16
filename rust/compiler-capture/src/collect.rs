use crate::{
    arguments::{self, Mode},
    config::Config,
    process,
    storage::{self, Invocation},
};
use serde_json::json;
use std::{
    env,
    ffi::{OsStr, OsString},
    fs, io,
    os::unix::{ffi::OsStrExt, process::ExitStatusExt},
    path::{Path, PathBuf},
    process::ExitStatus,
};

fn known_native_assembler_option(option: &[u8]) -> bool {
    if [
        b"--fatal-warnings".as_slice(),
        b"--compress-debug-sections",
        b"--nocompress-debug-sections",
    ]
    .contains(&option)
    {
        return true;
    }
    option
        .strip_prefix(b"--compress-debug-sections=")
        .is_some_and(|kind| {
            [
                b"none".as_slice(),
                b"zlib",
                b"zlib-gnu",
                b"zlib-gabi",
                b"zstd",
            ]
            .contains(&kind)
        })
}

fn native_assembler_options(arguments: &[OsString]) -> bool {
    let mut index = 0;
    while index < arguments.len() {
        let argument = arguments[index].as_bytes();
        if argument == b"--" {
            break;
        }
        if let Some(options) = argument.strip_prefix(b"-Wa,") {
            if !options
                .split(|byte| *byte == b',')
                .all(known_native_assembler_option)
            {
                return false;
            }
        } else if argument == b"-Xassembler" {
            index += 1;
            if !arguments
                .get(index)
                .is_some_and(|option| known_native_assembler_option(option.as_bytes()))
            {
                return false;
            }
        }
        index += 1;
    }
    true
}

fn role(path: &Path) -> &'static str {
    match path.extension().map(OsStr::as_bytes) {
        Some(b"i" | b"ii" | b"iim") => "preprocessing",
        Some(b"pcm" | b"gcm") => "module",
        Some(b"s" | b"S") => "assembly",
        Some(b"bc" | b"ll") => "ir",
        Some(b"o" | b"obj") => "object",
        _ => "auxiliary",
    }
}

fn eligible(analysis: &arguments::Analysis) -> bool {
    if analysis.inputs.len() != 1 || analysis.lto || !analysis.incomplete.is_empty() {
        return false;
    }
    matches!(
        analysis.input_language(0),
        Some(b"c" | b"c++" | b"cpp-output" | b"c++-cpp-output")
    ) && matches!(analysis.mode, Mode::Compile | Mode::Assembly | Mode::Link)
        && analysis.language.as_deref().is_none_or(|l| {
            [b"c".as_slice(), b"c++", b"cpp-output", b"c++-cpp-output"].contains(&l)
        })
}

fn link_operand(input: &[u8]) -> bool {
    let path = Path::new(OsStr::from_bytes(input));
    matches!(
        path.extension().map(OsStr::as_bytes),
        Some(b"o" | b"lo" | b"a" | b"so" | b"obj" | b"ld" | b"lds")
    ) || path
        .file_name()
        .is_some_and(|name| name.as_bytes().windows(4).any(|part| part == b".so."))
}

fn primary(analysis: &arguments::Analysis) -> Option<PathBuf> {
    if let Some(output) = &analysis.output {
        return Some(PathBuf::from(OsStr::from_bytes(output)));
    }
    if analysis.pch && analysis.mode == Mode::Compile && analysis.inputs.len() == 1 {
        return Some(PathBuf::from(OsStr::from_bytes(
            &[analysis.inputs[0].as_slice(), b".gch"].concat(),
        )));
    }
    if analysis.mode == Mode::Link {
        return Some("a.out".into());
    }
    if analysis.module_only && analysis.inputs.len() == 1 {
        return Path::new(OsStr::from_bytes(&analysis.inputs[0]))
            .file_name()
            .map(|name| Path::new(name).with_extension("pcm"));
    }
    if matches!(analysis.mode, Mode::Compile | Mode::Assembly) && analysis.inputs.len() == 1 {
        let input = Path::new(OsStr::from_bytes(&analysis.inputs[0]));
        return input.file_name().map(|name| {
            Path::new(name).with_extension(if analysis.mode == Mode::Compile {
                "o"
            } else {
                "s"
            })
        });
    }
    None
}

fn trace_eligible(analysis: &arguments::Analysis) -> bool {
    analysis.incomplete.is_empty()
        && !analysis.inputs.is_empty()
        && matches!(analysis.mode, Mode::Compile | Mode::Assembly | Mode::Link)
        && analysis.inputs.len() <= 128
        && analysis.inputs.iter().enumerate().all(|(index, input)| {
            analysis.input_language(index).is_some_and(|language| {
                [
                    b"c".as_slice(),
                    b"c++",
                    b"cpp-output",
                    b"c++-cpp-output",
                    b"assembler",
                    b"assembler-with-cpp",
                    b"c-header",
                    b"c++-header",
                    b"c++-module",
                    b"c++-module-cpp-output",
                    b"pcm",
                ]
                .contains(&language)
            }) || analysis.mode == Mode::Link && link_operand(input)
        })
}

fn primary_role(analysis: &arguments::Analysis) -> &'static str {
    if analysis.mode == Mode::Preprocess {
        "preprocessing"
    } else if analysis.pch {
        "pch"
    } else if analysis.module_only {
        "module"
    } else if analysis.emit_llvm {
        "ir"
    } else if analysis.mode == Mode::Compile || analysis.mode == Mode::Link && analysis.relocatable
    {
        "object"
    } else if analysis.mode == Mode::Assembly {
        "assembly"
    } else {
        "linked-output"
    }
}

pub fn run(config: &Config, original: &[OsString], root: &Path) -> io::Result<ExitStatus> {
    let cwd = env::current_dir()?;
    let mut invocation = match Invocation::start(root) {
        Ok(value) => value,
        Err(error) => {
            eprintln!("compiler-capture: capture initialization failed: {error}");
            return process::supervise(&config.executable, original);
        }
    };
    let prepared =
        crate::response::prepare(original, &invocation.aux, config.nix_response_default)?;
    let mut stream_response = None;
    let mut buffered_response = None;
    let mut response_errors = Vec::new();
    let effective = match prepared {
        crate::response::Prepared::Original => original.to_vec(),
        crate::response::Prepared::Buffered { arguments, path } => {
            buffered_response = Some(path);
            arguments
        }
        crate::response::Prepared::Streaming {
            source,
            prefix,
            error,
        } => {
            stream_response = Some((source, prefix));
            response_errors.push(error);
            original.to_vec()
        }
    };
    let version = config
        .version
        .split('.')
        .take(2)
        .map(|v| v.parse::<u32>().unwrap_or(0))
        .collect::<Vec<_>>();
    let legacy_response = config.family == "clang" && version.as_slice() < [3, 9].as_slice();
    let tool = config.backend == "tool-observe";
    let mut analysis = if stream_response.is_some() {
        arguments::analyze(&[], &cwd)
    } else if tool {
        arguments::analyze_tool(&effective, &cwd, &config.family)
    } else {
        arguments::analyze_dialect(&effective, &cwd, legacy_response)
    };
    if config.driver.ends_with("cpp") && analysis.mode == Mode::Link {
        analysis.mode = Mode::Preprocess;
    }
    let mut errors = analysis.incomplete.clone();
    let direct_llvm = tool
        && config.family == "linker"
        && analysis.linker_plugin.as_ref().is_some_and(|path| {
            Path::new(OsStr::from_bytes(path)).file_name() == Some(OsStr::new("LLVMgold.so"))
        });
    analysis.lto |= direct_llvm;
    let mut emits_link_ir = false;
    for argument in &analysis.expanded {
        let bytes = argument.as_bytes();
        let options = bytes
            .strip_prefix(b"-Wl,")
            .map(|value| value.split(|byte| *byte == b',').collect::<Vec<_>>())
            .unwrap_or_else(|| vec![bytes]);
        for option in options {
            if [
                b"-plugin-opt=emit-llvm".as_slice(),
                b"--plugin-opt=emit-llvm",
                b"--lto-emit-llvm",
            ]
            .contains(&option)
            {
                emits_link_ir = true;
            } else if [
                b"-plugin-opt=save-temps".as_slice(),
                b"--plugin-opt=save-temps",
                b"--save-temps",
            ]
            .contains(&option)
            {
                emits_link_ir = false;
            }
        }
    }
    errors.extend(response_errors);
    let mut args = effective;
    let mut added: Vec<OsString> = Vec::new();
    let mut map_snapshot = None;
    let query = matches!(
        analysis.mode,
        Mode::Query | Mode::Dependencies | Mode::Syntax
    );
    let stdout = (analysis.mode == Mode::Preprocess
        && analysis.output.as_deref().is_none_or(|p| p == b"-")
        || analysis.mode == Mode::Assembly && analysis.output.as_deref() == Some(b"-"))
        && unsafe { libc::isatty(libc::STDOUT_FILENO) } == 0;
    let preprocess_file = analysis.mode == Mode::Preprocess && analysis.output.is_some();
    let link_only = analysis.mode == Mode::Link
        && !analysis.inputs.is_empty()
        && analysis.inputs.iter().all(|input| link_operand(input));
    let candidate = eligible(&analysis);
    let c_family = (0..analysis.inputs.len()).any(|index| {
        matches!(
            analysis.input_language(index),
            Some(b"c" | b"c++" | b"cpp-output" | b"c++-cpp-output")
        )
    });
    let correct_cfi = config.family == "clang"
        && c_family
        && !analysis.saved_temps
        && version.first() == Some(&3)
        && (4..=9).contains(version.get(1).unwrap_or(&0));
    let external_cfi = correct_cfi
        && version.as_slice() == [3, 8]
        && (!config.target.starts_with("x86_64-")
            || analysis.expanded.iter().any(|argument| argument == "-m32"));
    let riscv_alignment = config.family == "clang"
        && config.target.starts_with("riscv64-")
        && version
            .first()
            .is_some_and(|major| (14..=16).contains(major))
        && !analysis.saved_temps
        && !analysis.expanded.iter().any(|argument| {
            argument.as_bytes().starts_with(b"-falign-functions")
                || argument.as_bytes().starts_with(b"-fno-align-functions")
        });
    let cxx = analysis.cxx_inputs(config.driver.ends_with("++"));
    // These driver defaults were measured with the actual legacy compilers.
    // Their external assembler already consumes text in a normal build.
    let default_integrated = !(config.target.starts_with("mips")
        && version.as_slice() < [8, 0].as_slice()
        || config.target.starts_with("powerpc") && version.as_slice() < [3, 6].as_slice()
        || config.target.starts_with("arm") && version.as_slice() < [3, 5].as_slice());
    let integrated = analysis
        .expanded
        .iter()
        .fold(default_integrated, |enabled, arg| match arg.as_bytes() {
            b"-fintegrated-as" | b"-integrated-as" => true,
            b"-fno-integrated-as" | b"-no-integrated-as" => false,
            _ => enabled,
        });
    let native_from_ir = config.family == "clang"
        && integrated
        && c_family
        && matches!(analysis.mode, Mode::Compile | Mode::Link)
        && !analysis.pch
        && !analysis.lto
        && !analysis.emit_llvm
        && !analysis.saved_temps;
    let native_from_ir = native_from_ir && native_assembler_options(&analysis.expanded);
    let external_cfi = external_cfi && !native_from_ir;
    let original_debug = analysis.debug.iter().fold(false, |enabled, argument| {
        if argument == b"-g0" {
            false
        } else if [
            b"-g".as_slice(),
            b"-g1",
            b"-g2",
            b"-g3",
            b"-gline-tables-only",
            b"-gline-directives-only",
            b"-gmlt",
        ]
        .contains(&argument.as_slice())
            || argument.starts_with(b"-ggdb")
            || argument.starts_with(b"-gdwarf")
        {
            true
        } else {
            enabled
        }
    });
    // Legacy Clang puts its debug-only CFI selection after the procedures.
    // GNU as then emits unwanted unwind data when capture adds -g. Preserve
    // that compiler behavior when the caller already requested debug data.
    let correct_cfi = correct_cfi && (integrated || !original_debug);
    let arm45_cfi = config.family == "gcc"
        && version.as_slice() == [4, 5]
        && config.target.starts_with("arm")
        && cxx
        && !original_debug
        && !analysis.saved_temps
        && config.debug_policy == "required"
        && !analysis.expanded.iter().any(|argument| {
            [
                b"-fno-exceptions".as_slice(),
                b"-fno-unwind-tables",
                b"-fno-asynchronous-unwind-tables",
            ]
            .contains(&argument.as_bytes())
        });
    let legacy_lto = config.family == "gcc"
        && analysis.mode == Mode::Link
        && !analysis.saved_temps
        // A driver-wide dumpdir also moves caller-requested split DWARF.
        // Keep its original prefix and contain LTO at the coordinator instead.
        && (version.first().copied().unwrap_or(0) < 11 || analysis.split_dwarf);
    #[cfg(target_arch = "x86_64")]
    let descriptor_available = storage::descriptor_available();
    #[cfg(not(target_arch = "x86_64"))]
    let descriptor_available = false;
    let trace_candidate = descriptor_available
        && (["gcc-trace", "clang-trace"].contains(&config.backend.as_str())
            && trace_eligible(&analysis)
            || direct_llvm && analysis.incomplete.is_empty())
        && storage::bounded_generation_filesystem(root);
    let generate_candidate = descriptor_available
        && config.backend == "gcc-dumpdir"
        && config.family == "gcc"
        && config.version == "15.2.0"
        && candidate
        && !analysis.saved_temps
        && storage::bounded_generation_filesystem(root);
    let tool_link = tool
        && config.family == "linker"
        && analysis.mode == Mode::Link
        && storage::bounded_generation_filesystem(root);
    let directory_view = if trace_candidate || generate_candidate || tool_link {
        match storage::DirectoryView::acquire(&invocation) {
            Ok(view) => Some(view),
            Err(error) => {
                errors.push(format!("capture directory view: {error}"));
                None
            }
        }
    } else {
        None
    };
    let tracing = trace_candidate && directory_view.is_some();
    let generate = generate_candidate && directory_view.is_some();
    if tracing && !tool {
        // A delimiter inside a response file would turn appended options into
        // filenames. Use the already parsed argument view only in that case.
        if args.iter().any(|arg| arg.as_bytes().starts_with(b"@"))
            && analysis.expanded.iter().any(|arg| arg == "--")
        {
            args = analysis.expanded.clone();
        }
        // GCC's linker plugin can discover LTO in existing objects or archives
        // without a driver -flto option. Prepare retention for every GCC link;
        // the coordinator is observed below to record whether LTO ran.
        if (!link_only || analysis.lto || config.family == "gcc") && !analysis.saved_temps {
            added.push("-save-temps".into());
        }
        if analysis.mode == Mode::Link
            && config.family == "gcc"
            && !legacy_lto
            && !analysis.saved_temps
        {
            added.extend([
                OsString::from("-dumpdir"),
                directory_view
                    .as_ref()
                    .unwrap()
                    .path
                    .join("lto-")
                    .into_os_string(),
            ]);
        }
        if analysis.lto && analysis.mode == Mode::Link && config.family == "clang" && !emits_link_ir
        {
            added.push("-Wl,-plugin-opt=save-temps".into());
        }
        if config.family == "clang" {
            let major = config
                .version
                .split('.')
                .next()
                .and_then(|v| v.parse::<u32>().ok())
                .unwrap_or(0);
            if major >= 10 {
                added.push("-fno-integrated-cc1".into());
            }
            if (analysis.pch || analysis.module_only) && version.as_slice() >= [3, 8].as_slice() {
                // A serialized AST must remain usable after this invocation's
                // saved preprocessing directory is released.
                added.extend(["-Xclang".into(), "-fmodules-embed-all-files".into()]);
            }
            if external_cfi {
                added.push("-fno-integrated-as".into());
                if let Some(search) = &config.assembler_search {
                    let mut option = OsString::from("-B");
                    option.push(search);
                    added.push(option);
                }
                if config.target.starts_with("i686-") {
                    // LLVM's i686 assembler uses long NOPs for alignment.
                    // GNU as defaults to i386 LEA padding even for --32.
                    // Match the original padding and omit gas's automatic
                    // used-ISA note, which the original assembler lacks.
                    added.push("-Wa,-mtune=pentiumpro,-mx86-used-note=no".into());
                }
            }
            if config.debug_policy == "required" {
                added.extend([OsString::from("-g"), OsString::from("-gdwarf-4")]);
            }
        } else if config.debug_policy == "required" {
            added.extend([OsString::from("-g"), OsString::from("-gdwarf-2")]);
        }
        if analysis.mode == Mode::Link && !emits_link_ir {
            if let Some(path) = &analysis.link_map {
                map_snapshot = Some((cwd.join(OsStr::from_bytes(path)), "cwd", path.clone()));
            } else {
                // The descriptor resolves to a private directory per invocation.
                // A stable name also avoids hashing transient Nix response names.
                let name = "linker.map".to_string();
                let mut option = OsString::from("-Wl,-Map,");
                option.push(directory_view.as_ref().unwrap().path.join(&name));
                added.push(option);
                map_snapshot = Some((
                    invocation.aux.join(&name),
                    "invocation-stage",
                    name.into_bytes(),
                ));
            }
        }
        let position = args
            .iter()
            .position(|arg| arg == "--")
            .unwrap_or(args.len());
        args.splice(position..position, added.clone());
    } else if generate {
        added.extend([
            OsString::from("-save-temps=obj"),
            OsString::from("-dumpdir"),
        ]);
        let mut directory = directory_view
            .as_ref()
            .unwrap()
            .path
            .clone()
            .into_os_string();
        directory.push("/");
        added.push(directory);
        args.extend(added.clone());
    } else if tool {
        if direct_llvm && tracing && !emits_link_ir {
            added.push("-plugin-opt=save-temps".into());
        } else if direct_llvm && !tracing {
            errors.push("direct LLVM plugin requires bounded subprocess tracing".into());
        }
        if config.family == "assembler" && !query && config.debug_policy == "required" {
            added.push("--gdwarf-2".into());
        }
        if config.family == "linker" && !query && !emits_link_ir {
            if let Some(path) = &analysis.link_map {
                map_snapshot = Some((cwd.join(OsStr::from_bytes(path)), "cwd", path.clone()));
            } else if let Some(view) = &directory_view {
                let mut option = OsString::from("-Map=");
                option.push(view.path.join("linker.map"));
                added.push(option);
                map_snapshot = Some((
                    invocation.aux.join("linker.map"),
                    "invocation-stage",
                    b"linker.map".to_vec(),
                ));
            } else {
                errors.push("direct linker map requires bounded capture scratch".into());
            }
        }
        let position = args
            .iter()
            .position(|arg| arg == "--")
            .unwrap_or(args.len());
        args.splice(position..position, added.clone());
    } else if !query && !stdout && !preprocess_file && !link_only {
        errors.push("no verified isolated backend for this invocation shape".into());
    }
    let mut artifacts = Vec::new();
    let mut pch_sources = Vec::new();
    let mut pch_inputs = Vec::new();
    for pair in analysis
        .expanded
        .windows(2)
        .filter(|pair| pair[0] == "-include-pch")
    {
        match invocation.snapshot(&cwd.join(&pair[1]), "pch", "cwd", pair[1].as_bytes()) {
            Ok(artifact) => {
                match crate::pch::sources(&invocation, &artifact) {
                    Ok(sources) => pch_sources.extend(sources),
                    Err(error) => errors.push(format!("PCH source index: {error}")),
                }
                artifacts.push(artifact);
            }
            Err(error) => errors.push(format!("PCH input snapshot: {error}")),
        }
    }
    if tool && !query {
        for input in &analysis.inputs {
            if input == b"-" {
                errors.push("direct assembler stdin has no saved assembly input".into());
                continue;
            }
            match invocation.snapshot(
                &cwd.join(OsStr::from_bytes(input)),
                if config.family == "assembler" {
                    "assembly"
                } else {
                    "object"
                },
                "cwd",
                input,
            ) {
                Ok(artifact) => artifacts.push(artifact),
                Err(error) => errors.push(format!("direct tool input snapshot: {error}")),
            }
        }
    }
    let mut subprocesses = Vec::new();
    let mut transformations = Vec::new();
    let mut recorded_arguments = original
        .iter()
        .map(|a| storage::hex(a.as_bytes()))
        .collect::<Vec<_>>();
    let wrapper_response = buffered_response
        .or_else(|| arguments::nix_response_file(original, config.nix_response_default));
    for path in &analysis.response_files {
        if wrapper_response
            .as_ref()
            .and_then(|p| fs::canonicalize(p).ok())
            .as_ref()
            == Some(path)
        {
            continue;
        }
        match invocation.snapshot(path, "response", "cwd", path.as_os_str().as_bytes()) {
            Ok(artifact) => artifacts.push(artifact),
            Err(error) => errors.push(format!("response snapshot: {error}")),
        }
    }
    if let Some(path) = wrapper_response {
        match invocation.snapshot(&path, "response", "wrapper-response", b"cc-params.rsp") {
            Ok(artifact) => {
                artifacts.push(artifact);
                recorded_arguments = vec![storage::hex(b"@${NIX_RESPONSE_FILE}")];
            }
            Err(err) => errors.push(format!("wrapper response snapshot: {err}")),
        }
    }
    let status_result = if let Some((source, prefix)) = stream_response {
        crate::response::forward(&config.executable, source, prefix)
    } else if stdout && !tracing {
        match invocation.start_stream() {
            Ok(mut stream) => {
                match process::stdout_capture(&config.executable, &args, |data| {
                    invocation.stream_chunk(&mut stream, data)
                }) {
                    Ok((status, None)) => {
                        match invocation.finish_stream(stream) {
                            Ok(mut artifact) => {
                                if analysis.mode == Mode::Assembly {
                                    artifact.role = "assembly".into();
                                }
                                artifacts.push(artifact);
                            }
                            Err(err) => errors.push(format!("stdout snapshot: {err}")),
                        }
                        Ok(status)
                    }
                    Ok((status, Some(error))) => {
                        let _ = invocation.discard_stream(stream);
                        errors.push(format!("stdout capture: {error}"));
                        Ok(status)
                    }
                    Err(err) => {
                        let _ = invocation.discard_stream(stream);
                        Err(err)
                    }
                }
            }
            Err(err) => {
                errors.push(format!("stdout capture initialization: {err}"));
                process::supervise(&config.executable, &args)
            }
        }
    } else if tracing {
        #[cfg(target_arch = "x86_64")]
        {
            let primaries = if let Some(primary) = primary(&analysis) {
                vec![primary]
            } else {
                analysis
                    .inputs
                    .iter()
                    .map(|input| {
                        Path::new(OsStr::from_bytes(input)).file_name().map(|name| {
                            Path::new(name).with_extension(if analysis.mode == Mode::Compile {
                                "o"
                            } else {
                                "s"
                            })
                        })
                    })
                    .collect::<Option<Vec<_>>>()
                    .unwrap_or_default()
            };
            crate::trace::run(
                &config.executable,
                &args,
                &mut invocation,
                crate::trace::Options {
                    primaries: &primaries,
                    nix_response_default: config.nix_response_default,
                    primary_role: primary_role(&analysis),
                    redirect: !analysis.saved_temps,
                    view: &directory_view.as_ref().unwrap().path,
                    stdout_role: if stdout { Some("assembly") } else { None },
                    correct_cfi_sections: if correct_cfi {
                        Some(crate::trace::CfiCorrection::DirectiveOrder)
                    } else if arm45_cfi {
                        Some(crate::trace::CfiCorrection::Arm45Sections)
                    } else if riscv_alignment {
                        Some(
                            if version.first() == Some(&14)
                                && analysis
                                    .expanded
                                    .iter()
                                    .rfind(|arg| arg.as_bytes().starts_with(b"-O"))
                                    .is_none_or(|arg| arg == "-O0")
                            {
                                crate::trace::CfiCorrection::RiscvNoRelax
                            } else {
                                crate::trace::CfiCorrection::RiscvAlignment
                            },
                        )
                    } else {
                        None
                    },
                    legacy_lto,
                    redirect_debug_objects: config.family == "gcc"
                        && analysis.mode == Mode::Link
                        && config.debug_policy == "required"
                        && !original_debug,
                    lto_inputs: if direct_llvm
                        || config.family == "clang" && analysis.lto && analysis.mode == Mode::Link
                    {
                        &analysis.inputs
                    } else {
                        &[]
                    },
                    pch_sources: &pch_sources,
                    pch_producer: analysis.pch,
                    native_from_ir,
                    x86_tuning: config.family == "clang"
                        && version.first().is_some_and(|major| *major >= 18)
                        && (config.target.starts_with("x86_64-")
                            || config.target.starts_with("i686-")),
                    split_dwarf: analysis.split_dwarf,
                    normalize_directives_only: config.family == "clang"
                        && analysis
                            .expanded
                            .iter()
                            .any(|arg| arg == "-fdirectives-only"),
                    gcc_directives_only: config.family == "gcc"
                        && analysis
                            .expanded
                            .iter()
                            .any(|arg| arg == "-fdirectives-only"),
                    root_linker: tool && config.family == "linker",
                },
            )
            .map(|outcome| {
                analysis.lto |= outcome.lto_started;
                pch_inputs = outcome.pch_inputs;
                artifacts.extend(outcome.artifacts);
                subprocesses = outcome.subprocesses;
                transformations = outcome.transformations;
                errors.extend(outcome.errors);
                outcome.status
            })
        }
        #[cfg(not(target_arch = "x86_64"))]
        {
            unreachable!()
        }
    } else {
        process::supervise(&config.executable, &args)
    };
    let status = match status_result {
        Ok(value) => value,
        Err(error) => {
            // A failed spawn cannot stand in for an ordinary compiler error.
            // Keep the pending directory so finalization also diagnoses it.
            return Err(error);
        }
    };
    process::check_cancelled().ok();
    if tool {
        let metadata = json!({"schema":1,"executable_hex":storage::hex(config.executable.as_os_str().as_bytes()),
            "arguments_hex":args.iter().map(|arg|storage::hex(arg.as_bytes())).collect::<Vec<_>>()});
        match invocation.metadata(&metadata) {
            Ok(artifact) => artifacts.push(artifact),
            Err(error) => errors.push(format!("direct tool command snapshot: {error}")),
        }
    }
    if let Some(primary) = primary(&analysis).filter(|_| !query && !stdout && status.success()) {
        let path = cwd.join(&primary);
        let is_input = analysis.inputs.iter().any(|input| {
            fs::canonicalize(cwd.join(OsStr::from_bytes(input))).ok()
                == fs::canonicalize(&path).ok()
        });
        if path.is_file() && !is_input {
            let kind = primary_role(&analysis);
            match invocation.snapshot(&path, kind, "cwd", primary.as_os_str().as_bytes()) {
                Ok(item) => artifacts.push(item),
                Err(err) => errors.push(format!("primary snapshot: {err}")),
            }
        } else if status.success() && !query && primary.as_os_str() != OsStr::new("/dev/null") {
            errors.push("primary output is missing, is an input, or is not a regular file".into());
        }
    }
    if generate {
        let entries = match fs::read_dir(&invocation.aux)
            .and_then(|entries| entries.take(64).collect::<Result<Vec<_>, _>>())
        {
            Ok(entries) => entries,
            Err(err) => {
                errors.push(format!("auxiliary enumeration: {err}"));
                vec![]
            }
        };
        if entries.len() == 64 {
            errors.push("too many compiler-generated auxiliary files".into());
        }
        for entry in entries {
            let path = entry.path();
            if entry.file_name() == "wrapper-response.rsp" {
                continue;
            }
            if !entry.file_type().is_ok_and(|kind| kind.is_file()) {
                errors.push("non-regular auxiliary artifact".into());
                continue;
            }
            match invocation.snapshot(
                &path,
                role(&path),
                "invocation-stage",
                entry.file_name().as_bytes(),
            ) {
                Ok(item) => artifacts.push(item),
                Err(err) => errors.push(format!("auxiliary snapshot: {err}")),
            }
        }
        if status.success() {
            if !artifacts.iter().any(|a| a.role == "assembly") {
                errors.push("assembly was not emitted".into());
            }
            let preprocessed = analysis
                .inputs
                .first()
                .is_some_and(|input| input.ends_with(b".i") || input.ends_with(b".ii"));
            if !preprocessed && !artifacts.iter().any(|a| a.role == "preprocessing") {
                errors.push("preprocessing was not emitted".into());
            }
        }
        if artifacts.iter().any(|a| a.role == "auxiliary") {
            errors.push("unclassified compiler auxiliary output".into());
        }
    }
    if let Some((path, base, name)) = map_snapshot.filter(|_| status.success()) {
        match invocation.snapshot(&path, "link-map", base, &name) {
            Ok(artifact) => artifacts.push(artifact),
            Err(err) => errors.push(format!("link map snapshot: {err}")),
        }
    }
    if tracing && analysis.lto && analysis.mode == Mode::Link {
        // The LTO coordinator creates response files, the WPA partition list,
        // resolution data and GIMPLE objects without a subprocess -o operand.
        // dumpdir confines all of them to the same bounded invocation directory.
        let entries = fs::read_dir(&invocation.aux)?
            .take(257)
            .collect::<Result<Vec<_>, _>>()?;
        if entries.len() > 256 {
            errors.push("too many LTO auxiliary files".into());
        }
        for entry in entries.into_iter().take(256) {
            let name = entry.file_name();
            let bytes = name.as_bytes();
            if config.family == "gcc"
                && !(bytes.starts_with(b"lto-")
                    || bytes.starts_with(b"gcc-lto")
                    || bytes.starts_with(b"gcc-resolution"))
            {
                continue;
            }
            if bytes == b"wrapper-response.rsp" || bytes == b"linker.map" {
                continue;
            }
            let kind = match entry.path().extension().map(OsStr::as_bytes) {
                Some(b"i" | b"ii") => "preprocessing",
                Some(b"s") => "assembly",
                Some(b"o") => "object",
                Some(extension)
                    if extension.strip_prefix(b"o").is_some_and(|tail| {
                        !tail.is_empty() && tail.iter().all(u8::is_ascii_digit)
                    }) =>
                {
                    "object"
                }
                Some(b"bc" | b"ll") => "ir",
                _ if bytes.windows(6).any(|part| part == b".lto.o") => "object",
                _ if bytes.ends_with(b"res") || bytes.ends_with(b"resolution.txt") => {
                    "link-resolution"
                }
                _ if bytes.windows(6).any(|part| part == b".args.")
                    || bytes.ends_with(b"_args") =>
                {
                    "response"
                }
                _ => "lto-metadata",
            };
            match invocation.snapshot(&entry.path(), kind, "invocation-stage", bytes) {
                Ok(artifact) => {
                    let produced = artifacts.iter().any(|previous| {
                        previous.role == "object"
                            && previous.path_base == "invocation-stage"
                            && previous.original_path_hex == artifact.original_path_hex
                    });
                    if (config.family == "clang" || direct_llvm) && kind == "object" && !produced {
                        use std::io::Read;
                        let mut magic = [0u8; 4];
                        if fs::File::open(invocation.root.join("objects").join(&artifact.sha256))
                            .and_then(|mut file| file.read_exact(&mut magic))
                            .is_ok()
                            && magic == *b"\x7fELF"
                        {
                            let mut native = artifact.clone();
                            native.role = "lto-native-object".into();
                            artifacts.push(native);
                        }
                    }
                    artifacts.push(artifact);
                }
                Err(error) => errors.push(format!("LTO snapshot: {error}")),
            }
        }
    }
    if tracing && status.success() && !link_only {
        for (index, input) in analysis.inputs.iter().enumerate() {
            let role = match analysis.input_language(index) {
                Some(b"assembler") => "assembly",
                Some(b"cpp-output" | b"c++-cpp-output" | b"c++-module-cpp-output") => {
                    "preprocessing"
                }
                _ => continue,
            };
            let path = cwd.join(OsStr::from_bytes(input));
            match invocation.snapshot(&path, role, "cwd", input) {
                Ok(artifact) => artifacts.push(artifact),
                Err(err) => errors.push(format!("{role} input snapshot: {err}")),
            }
        }
        let representation = if analysis.pch {
            "pch"
        } else if analysis.module_only {
            "module"
        } else if native_from_ir || analysis.emit_llvm || analysis.lto && config.family == "clang" {
            "ir"
        } else {
            "assembly"
        };
        let discarded_assembly =
            analysis.mode == Mode::Assembly && analysis.output.as_deref() == Some(b"/dev/null");
        if !discarded_assembly && !artifacts.iter().any(|a| a.role == representation) {
            errors.push(format!("traced compilation did not emit {representation}"));
        }
        let preprocessed = analysis.preprocessed_inputs();
        if !preprocessed && !artifacts.iter().any(|a| a.role == "preprocessing") {
            errors.push("traced compilation did not emit preprocessing".into());
        }
        if artifacts.iter().any(|a| a.role == "auxiliary") {
            errors.push("unclassified compiler auxiliary output".into());
        }
    }
    if analysis.mode == Mode::Link && status.success() && !query {
        if let Some(primary) = primary(&analysis).filter(|path| path.as_os_str() != "/dev/null") {
            use io::Read;
            let path = cwd.join(&primary);
            let mut header = [0u8; 18];
            let kind = fs::File::open(&path)
                .and_then(|mut file| file.read_exact(&mut header))
                .ok()
                .and_then(|_| {
                    if header[..4] == *b"BC\xc0\xde" || header[..4] == [0xde, 0xc0, 0x17, 0x0b] {
                        Some("ir")
                    } else if header[..4] == *b"\x7fELF" && [1, 2].contains(&header[5]) {
                        let kind = if header[5] == 1 {
                            u16::from_le_bytes([header[16], header[17]])
                        } else {
                            u16::from_be_bytes([header[16], header[17]])
                        };
                        match kind {
                            1 => Some("object"),
                            2 | 3 => Some("linked-output"),
                            _ => None,
                        }
                    } else {
                        None
                    }
                });
            if let Some(kind) = kind {
                analysis.link_output_role = Some(kind.into());
                let names = [
                    storage::hex(primary.as_os_str().as_bytes()),
                    storage::hex(path.as_os_str().as_bytes()),
                ];
                for artifact in &mut artifacts {
                    if artifact.role == "linked-output"
                        || artifact.path_base == "cwd"
                            && names.contains(&artifact.original_path_hex)
                    {
                        artifact.role = kind.into();
                    }
                }
            } else {
                errors.push("linked primary output has no verified ELF or LLVM IR type".into());
            }
        }
    }
    if tracing
        && status.success()
        && analysis.mode == Mode::Link
        && (direct_llvm || config.family == "clang" && analysis.lto)
        && analysis.link_output_role.as_deref() != Some("ir")
        && artifacts.iter().any(|artifact| artifact.role == "ir")
        && !artifacts
            .iter()
            .any(|artifact| artifact.role == "lto-native-object")
    {
        errors.push("LLVM LTO has no retained linker-generated native object".into());
    }
    artifacts.sort_by(|a, b| {
        (&a.role, &a.original_path_hex, &a.sha256).cmp(&(&b.role, &b.original_path_hex, &b.sha256))
    });
    if analysis.pch && status.success() {
        if let Some(path) = primary(&analysis) {
            let name = storage::hex(path.as_os_str().as_bytes());
            if let Some(pch) = artifacts.iter().find(|artifact| {
                artifact.role == "pch"
                    && artifact.path_base == "cwd"
                    && artifact.original_path_hex == name
            }) {
                if let Err(error) = crate::pch::remember(&mut invocation, pch, &pch_inputs) {
                    errors.push(format!("PCH dependency state: {error}"));
                }
            }
        }
    }
    // Runtime-unique staging paths are represented relative to the invocation,
    // so completion order/PIDs do not become part of canonical records.
    let normalized_added = added
        .iter()
        .map(|arg| {
            if arg
                .as_bytes()
                .starts_with(invocation.aux.as_os_str().as_bytes())
            {
                storage::hex(b"${INVOCATION_STAGE}/")
            } else {
                storage::hex(arg.as_bytes())
            }
        })
        .collect::<Vec<_>>();
    let config_bytes = serde_json::to_vec(config).expect("compiler configuration is serializable");
    let record = json!({"schema":1,"compiler":config,"config_sha256":storage::hash(&config_bytes),
        "native_from_ir":native_from_ir && tracing,
        "coverage_contract":if tracing {"experimental-ptrace-c-cxx"} else {"experimental-gcc15-single-c-cxx"}, "cwd_hex":storage::hex(cwd.as_os_str().as_bytes()),
        "arguments_hex":recorded_arguments,
        "added_arguments_hex":normalized_added,"analysis":analysis,"retained_pch_sources":pch_sources,
        "overrode_explicit_g0":tracing && config.debug_policy=="required" && analysis.debug.iter().any(|a|a==b"-g0"),
        "compiler_status":{"exit_code":status.code(),"signal":status.signal()},
        "capture_complete":errors.is_empty(),"capture_errors":errors,"artifacts":artifacts,"subprocesses":subprocesses,"limits":invocation.limits(),"transformations":transformations});
    if let Err(error) = invocation.commit(&record) {
        eprintln!("compiler-capture: record commit failed: {error}");
    }
    Ok(status)
}

#[cfg(test)]
mod tests {
    use super::native_assembler_options;
    use std::ffi::OsString;

    #[test]
    fn native_emission_retains_supported_assembler_controls() {
        for arguments in [
            vec!["-Wa,--compress-debug-sections"],
            vec!["-Wa,--fatal-warnings,--compress-debug-sections=zlib"],
            vec!["-Xassembler", "--compress-debug-sections=zlib-gnu"],
            vec!["-Wa,--nocompress-debug-sections"],
            vec!["--", "-Xassembler"],
        ] {
            let arguments = arguments
                .into_iter()
                .map(OsString::from)
                .collect::<Vec<_>>();
            assert!(native_assembler_options(&arguments));
        }
    }

    #[test]
    fn native_emission_does_not_discard_unknown_assembler_semantics() {
        for arguments in [
            vec!["-Wa,--compress-debug-sections,--defsym,answer=7"],
            vec!["-Xassembler", "--noexecstack"],
            vec!["-Xassembler"],
            vec!["-Wa,"],
            vec!["-Wa,--compress-debug-sections=unknown"],
        ] {
            let arguments = arguments
                .into_iter()
                .map(OsString::from)
                .collect::<Vec<_>>();
            assert!(!native_assembler_options(&arguments));
        }
    }
}
