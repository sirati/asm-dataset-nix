//! An analysis view only. The original OsStrings always go to the driver.
use serde::Serialize;
use std::{
    collections::HashSet,
    ffi::OsString,
    fs, io,
    os::unix::ffi::OsStrExt,
    path::{Path, PathBuf},
};

const MAX_RESPONSE_BYTES: usize = 1024 * 1024;
const MAX_ARGUMENTS: usize = 65536;
const MAX_DEPTH: usize = 16;

#[derive(Debug, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum Mode {
    Query,
    Preprocess,
    Dependencies,
    Syntax,
    Assembly,
    Compile,
    Link,
}

#[derive(Debug, Serialize)]
pub struct Analysis {
    pub mode: Mode,
    pub inputs: Vec<Vec<u8>>,
    pub output: Option<Vec<u8>>,
    pub language: Option<Vec<u8>>,
    pub input_languages: Vec<Option<Vec<u8>>>,
    pub saved_temps: bool,
    pub debug: Vec<Vec<u8>>,
    pub split_dwarf: bool,
    pub lto: bool,
    pub pch: bool,
    pub module_only: bool,
    pub relocatable: bool,
    pub emit_llvm: bool,
    pub linker_plugin: Option<Vec<u8>>,
    pub link_output_role: Option<String>,
    pub link_map: Option<Vec<u8>>,
    pub incomplete: Vec<String>,
    pub response_dialect: &'static str,
    #[serde(skip)]
    pub response_files: Vec<PathBuf>,
    #[serde(skip)]
    pub expanded: Vec<OsString>,
}

impl Analysis {
    pub fn cxx_inputs(&self, driver_cxx: bool) -> bool {
        self.inputs.iter().enumerate().any(|(index, _)| {
            let explicit = self.input_languages.get(index).and_then(Option::as_deref);
            if explicit.is_none() && driver_cxx {
                return true;
            }
            matches!(
                self.input_language(index),
                Some(
                    b"c++"
                        | b"c++-cpp-output"
                        | b"c++-header"
                        | b"c++-module"
                        | b"c++-module-cpp-output"
                )
            )
        })
    }
    pub fn input_language(&self, index: usize) -> Option<&[u8]> {
        if let Some(language) = self.input_languages.get(index).and_then(Option::as_deref) {
            return Some(language);
        }
        match Path::new(std::ffi::OsStr::from_bytes(&self.inputs[index]))
            .extension()
            .map(OsStrExt::as_bytes)
        {
            Some(b"c") => Some(b"c"),
            Some(b"C" | b"cc" | b"cp" | b"cpp" | b"CPP" | b"cxx" | b"c++") => Some(b"c++"),
            Some(b"i") => Some(b"cpp-output"),
            Some(b"ii") => Some(b"c++-cpp-output"),
            Some(b"iim") => Some(b"c++-module-cpp-output"),
            Some(b"cppm" | b"ccm" | b"cxxm" | b"c++m" | b"ixx") => Some(b"c++-module"),
            Some(b"pcm") => Some(b"pcm"),
            Some(b"s") => Some(b"assembler"),
            Some(b"S" | b"sx") => Some(b"assembler-with-cpp"),
            Some(b"h") => Some(b"c-header"),
            Some(b"hpp" | b"hh" | b"hxx") => Some(b"c++-header"),
            _ => None,
        }
    }

    pub fn preprocessed_inputs(&self) -> bool {
        !self.inputs.is_empty()
            && (0..self.inputs.len()).all(|index| {
                matches!(
                    self.input_language(index),
                    Some(
                        b"cpp-output"
                            | b"c++-cpp-output"
                            | b"c++-module-cpp-output"
                            | b"assembler"
                            | b"pcm"
                    )
                )
            })
    }
}

/// GCC/Unix Clang response syntax: whitespace, single/double quotes, and
/// backslash escapes. This is deliberately not shell parsing. In particular,
/// dollar expansions and ANSI-C shell quotes have no special interpretation.
#[cfg(test)]
fn tokenize(bytes: &[u8]) -> io::Result<Vec<OsString>> {
    tokenize_dialect(bytes, false)
}

fn tokenize_dialect(bytes: &[u8], legacy_clang: bool) -> io::Result<Vec<OsString>> {
    let mut words = Vec::new();
    let mut word = Vec::new();
    let mut quote = None;
    let mut escaped = false;
    let mut started = false;
    for (index, &b) in bytes.iter().enumerate() {
        if b == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "NUL in response file",
            ));
        }
        if escaped {
            word.push(b);
            escaped = false;
            started = true;
        } else if b == b'\\'
            && (!legacy_clang
                || quote.is_some_and(|q| {
                    bytes
                        .get(index + 1)
                        .is_some_and(|next| *next == q || *next == b'\\')
                }))
        {
            escaped = true;
            started = true;
        } else if quote == Some(b) {
            quote = None;
        } else if quote.is_none() && (b == b'\'' || b == b'"') {
            quote = Some(b);
            started = true;
        } else if quote.is_none() && b.is_ascii_whitespace() {
            if started {
                words.push(OsString::from(std::ffi::OsStr::from_bytes(&word)));
                word.clear();
                started = false;
            }
        } else {
            word.push(b);
            started = true;
        }
        if words.len() > MAX_ARGUMENTS {
            return Err(io::Error::other("too many response arguments"));
        }
    }
    // Some compiler versions accept unmatched quotes and trailing backslashes.
    // Until measured, do not pretend that this analysis matches their dialect.
    if escaped || quote.is_some() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "ambiguous response quoting",
        ));
    }
    if started {
        words.push(OsString::from(std::ffi::OsStr::from_bytes(&word)));
    }
    Ok(words)
}

struct ResponseExpansion {
    stack: HashSet<PathBuf>,
    budget: usize,
    out: Vec<OsString>,
    files: Vec<PathBuf>,
    legacy_clang: bool,
}

impl ResponseExpansion {
    fn expand(&mut self, args: &[OsString], cwd: &Path, depth: usize) -> io::Result<()> {
        if depth > MAX_DEPTH {
            return Err(io::Error::other("response nesting exceeds 16"));
        }
        for arg in args {
            let bytes = arg.as_bytes();
            if let Some(name) = bytes.strip_prefix(b"@") {
                let path = fs::canonicalize(cwd.join(std::ffi::OsStr::from_bytes(name)))?;
                if !self.stack.insert(path.clone()) {
                    return Err(io::Error::other("response file cycle"));
                }
                if self.files.len() >= 128 {
                    return Err(io::Error::other("response analysis exceeds 128 files"));
                }
                self.files.push(path.clone());
                use std::os::unix::fs::OpenOptionsExt;
                let mut file = fs::OpenOptions::new()
                    .read(true)
                    .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
                    .open(&path)?;
                if !file.metadata()?.is_file() {
                    return Err(io::Error::other(
                        "response analysis requires a regular file",
                    ));
                }
                let mut data = Vec::new();
                use io::Read;
                file.by_ref()
                    .take((self.budget + 1) as u64)
                    .read_to_end(&mut data)?;
                if data.len() > self.budget {
                    return Err(io::Error::other("response analysis exceeds 1 MiB"));
                }
                self.budget -= data.len();
                self.expand(&tokenize_dialect(&data, self.legacy_clang)?, cwd, depth + 1)?;
                self.stack.remove(&path);
            } else {
                self.out.push(arg.clone());
            }
            if self.out.len() > MAX_ARGUMENTS {
                return Err(io::Error::other("analysis exceeds 65536 arguments"));
            }
        }
        Ok(())
    }
}

pub fn analyze(original: &[OsString], cwd: &Path) -> Analysis {
    analyze_dialect(original, cwd, false)
}

pub fn analyze_dialect(original: &[OsString], cwd: &Path, legacy_clang: bool) -> Analysis {
    let mut analysis = Analysis {
        mode: Mode::Link,
        inputs: vec![],
        output: None,
        language: None,
        input_languages: vec![],
        saved_temps: false,
        debug: vec![],
        split_dwarf: false,
        lto: false,
        pch: false,
        module_only: false,
        relocatable: false,
        emit_llvm: false,
        linker_plugin: None,
        link_output_role: None,
        link_map: None,
        incomplete: vec![],
        response_dialect: if legacy_clang {
            "clang-before-3.9"
        } else {
            "gnu"
        },
        response_files: vec![],
        expanded: vec![],
    };
    let mut response = ResponseExpansion {
        stack: HashSet::new(),
        budget: MAX_RESPONSE_BYTES,
        out: Vec::new(),
        files: Vec::new(),
        legacy_clang,
    };
    let result = response.expand(original, cwd, 0);
    analysis.response_files = response.files;
    if let Err(err) = result {
        analysis
            .incomplete
            .push(format!("response analysis: {err}"));
        return analysis;
    }
    let args = response.out;
    let mut i = 0;
    let mut positional = false;
    let mut preprocess = false;
    let mut dependencies = false;
    let mut syntax = false;
    let mut assembly = false;
    let mut compile = false;
    let mut query = false;
    let mut language = None;
    let mut linker_options = Vec::new();
    while i < args.len() {
        let a = args[i].as_bytes();
        i += 1;
        if positional || a == b"-" || !a.starts_with(b"-") {
            analysis.inputs.push(a.to_vec());
            analysis.input_languages.push(language.clone());
            continue;
        }
        match a {
            _ if a.starts_with(b"-Wl,") => {
                linker_options.extend(a[4..].split(|byte| *byte == b',').map(Vec::from))
            }
            b"--" => positional = true,
            b"-c" => compile = true,
            b"--precompile" => {
                compile = true;
                analysis.module_only = true;
            }
            b"-r" | b"--relocatable" | b"-Wl,-r" | b"-Wl,--relocatable" => {
                analysis.relocatable = true
            }
            b"-emit-llvm" => analysis.emit_llvm = true,
            b"-S" => assembly = true,
            b"-E" => preprocess = true,
            b"-M" | b"-MM" => dependencies = true,
            b"-fsyntax-only" => syntax = true,
            b"--version"
            | b"-V"
            | b"-qversion"
            | b"-version"
            | b"-dumpversion"
            | b"-dumpfullversion"
            | b"-dumpmachine"
            | b"-dumpspecs"
            | b"-print-search-dirs"
            | b"-###"
            | b"--help"
            | b"--help-hidden"
            | b"--target-help" => query = true,
            b"-cc1" | b"-cc1as" => analysis
                .incomplete
                .push("internal compiler entry point".into()),
            b"-o"
            | b"--output"
            | b"-x"
            | b"--language"
            | b"-MF"
            | b"-MT"
            | b"-MQ"
            | b"-I"
            | b"-D"
            | b"-U"
            | b"-A"
            | b"--param"
            | b"-isystem"
            | b"-cxx-isystem"
            | b"-internal-isystem"
            | b"-internal-externc-isystem"
            | b"-include"
            | b"-include-pch"
            | b"-imacros"
            | b"-idirafter"
            | b"-iquote"
            | b"-iprefix"
            | b"-iwithprefix"
            | b"-iwithprefixbefore"
            | b"--sysroot"
            | b"-isysroot"
            | b"-L"
            | b"-l"
            | b"-B"
            | b"-target"
            | b"--target"
            | b"-arch"
            | b"-Xclang"
            | b"-Xlinker"
            | b"-Xassembler"
            | b"-Xpreprocessor"
            | b"-dumpdir"
            | b"-dumpbase"
            | b"-dumpbase-ext"
            | b"-auxbase"
            | b"-auxbase-strip" => {
                if i == args.len() {
                    analysis.incomplete.push("missing option operand".into());
                    break;
                }
                if a == b"-o" || a == b"--output" {
                    analysis.output = Some(args[i].as_bytes().to_vec());
                }
                if a == b"-x" || a == b"--language" {
                    analysis.language = Some(args[i].as_bytes().to_vec());
                    language = (args[i] != "none").then(|| args[i].as_bytes().to_vec());
                }
                if a == b"-Xclang" {
                    analysis.incomplete.push("unmeasured cc1 options".into());
                }
                if a == b"-Xlinker" {
                    linker_options.push(args[i].as_bytes().to_vec());
                }
                i += 1;
            }
            _ if a.starts_with(b"--output=") => analysis.output = Some(a[9..].to_vec()),
            _ if a.starts_with(b"--language=") => {
                analysis.language = Some(a[11..].to_vec());
                language = (&a[11..] != b"none").then(|| a[11..].to_vec());
            }
            _ if a.starts_with(b"-o") => analysis.output = Some(a[2..].to_vec()),
            _ if a.starts_with(b"-x") => {
                analysis.language = Some(a[2..].to_vec());
                language = (&a[2..] != b"none").then(|| a[2..].to_vec());
            }
            _ if a.starts_with(b"-save-temps") => analysis.saved_temps = true,
            _ if a.starts_with(b"-g") => {
                if a == b"-gsplit-dwarf" || a == b"-gsplit-dwarf=split" {
                    analysis.split_dwarf = true;
                } else if a == b"-gno-split-dwarf" || a == b"-gsplit-dwarf=single" {
                    analysis.split_dwarf = false;
                }
                analysis.debug.push(a.to_vec());
            }
            b"-fno-lto" => analysis.lto = false,
            _ if a.starts_with(b"-flto") => analysis.lto = true,
            _ if a.starts_with(b"-print-") || a.starts_with(b"--print-") => query = true,
            b"-fmodules"
            | b"-fcxx-modules"
            | b"-fmodules-ts"
            | b"-fimplicit-modules"
            | b"-fimplicit-module-maps"
            | b"-fmodule-output" => {}
            _ if a.starts_with(b"-fmodules-cache-path=")
                || a.starts_with(b"-fmodule-file=")
                || a.starts_with(b"-fmodule-output=")
                || a.starts_with(b"-fmodule-map-file=") => {}
            _ if a.starts_with(b"-fplugin")
                || a.starts_with(b"-fmodules")
                || a.starts_with(b"-fmodule-") =>
            {
                analysis
                    .incomplete
                    .push("unmeasured compiler plugin or module mode".into())
            }
            _ => (),
        }
    }
    let mut map_operand = false;
    for option in &linker_options {
        if map_operand {
            analysis.link_map = Some(option.clone());
            map_operand = false;
        } else if option == b"-Map" || option == b"--Map" {
            map_operand = true;
        } else if option == b"-r" || option == b"--relocatable" {
            analysis.relocatable = true;
        } else if let Some(path) = option
            .strip_prefix(b"-Map=")
            .or_else(|| option.strip_prefix(b"--Map="))
        {
            analysis.link_map = Some(path.to_vec());
        }
    }
    if map_operand {
        analysis
            .incomplete
            .push("missing linker map operand".into());
    }
    if analysis.inputs.is_empty()
        && analysis.output.is_none()
        && args.iter().any(|a| a == "-v" || a == "-Wl,--version")
    {
        query = true;
    }
    analysis.mode = if query {
        Mode::Query
    } else if dependencies {
        Mode::Dependencies
    } else if preprocess {
        Mode::Preprocess
    } else if syntax {
        Mode::Syntax
    } else if assembly {
        Mode::Assembly
    } else if compile {
        Mode::Compile
    } else {
        Mode::Link
    };
    analysis.pch = (0..analysis.inputs.len()).any(|index| {
        matches!(
            analysis.input_language(index),
            Some(b"c-header" | b"c++-header")
        )
    });
    if analysis.pch && analysis.mode == Mode::Link {
        analysis.mode = Mode::Compile;
    }
    analysis.expanded = args;
    analysis
}

pub fn nix_response_file(args: &[OsString], default: bool) -> Option<PathBuf> {
    if args.len() != 1 {
        return None;
    }
    let path = PathBuf::from(std::ffi::OsStr::from_bytes(
        args[0].as_bytes().strip_prefix(b"@")?,
    ));
    let name = path.file_name()?.as_bytes();
    let (suffix, variable, default) = if let Some(suffix) = name.strip_prefix(b"cc-params.") {
        (suffix, "NIX_CC_USE_RESPONSE_FILE", default)
    } else {
        (
            name.strip_prefix(b"ld-params.")?,
            "NIX_LD_USE_RESPONSE_FILE",
            true,
        )
    };
    let enabled = std::env::var(variable)
        .ok()
        .map(|value| value.parse::<u32>().unwrap_or(0) > 0)
        .unwrap_or(default);
    if !enabled {
        return None;
    }
    let temporary = PathBuf::from(std::env::var_os("TMPDIR").unwrap_or_else(|| "/tmp".into()));
    if suffix.len() == 6
        && suffix.iter().all(|b| b.is_ascii_alphanumeric())
        && path.parent() == Some(temporary.as_path())
    {
        Some(path)
    } else {
        None
    }
}

pub fn analyze_tool(original: &[OsString], cwd: &Path, family: &str) -> Analysis {
    let mut analysis = analyze(original, cwd);
    if !analysis.incomplete.is_empty() {
        return analysis;
    }
    analysis.inputs.clear();
    analysis.input_languages.clear();
    analysis.output = None;
    analysis.link_map = None;
    analysis.mode = if family == "assembler" {
        Mode::Compile
    } else {
        Mode::Link
    };
    let mut positional = false;
    let mut index = 0;
    let mut verbose = false;
    while index < analysis.expanded.len() {
        let arg = analysis.expanded[index].as_bytes();
        index += 1;
        if positional || arg == b"-" || !arg.starts_with(b"-") {
            analysis.inputs.push(arg.to_vec());
            analysis.input_languages.push(None);
            continue;
        }
        match arg {
            b"--" => positional = true,
            b"--version" | b"--help" => analysis.mode = Mode::Query,
            b"-v" | b"--verbose" => verbose = true,
            b"-o" | b"-Map" | b"--Map" | b"-m" | b"-e" | b"-T" | b"--script" | b"-u" | b"-L"
            | b"-l" | b"-I" | b"--defsym" | b"-rpath" | b"-rpath-link" | b"-soname"
            | b"--soname" | b"-dynamic-linker" | b"--dynamic-linker" | b"-z" | b"-plugin"
            | b"--plugin" => {
                let Some(value) = analysis.expanded.get(index) else {
                    analysis
                        .incomplete
                        .push("missing tool option operand".into());
                    break;
                };
                if arg == b"-o" {
                    analysis.output = Some(value.as_bytes().to_vec());
                }
                if arg == b"-Map" || arg == b"--Map" {
                    analysis.link_map = Some(value.as_bytes().to_vec());
                }
                if arg == b"-plugin" || arg == b"--plugin" {
                    analysis.linker_plugin = Some(value.as_bytes().to_vec());
                    if Path::new(value).file_name() != Some(std::ffi::OsStr::new("LLVMgold.so")) {
                        analysis
                            .incomplete
                            .push("unmeasured direct linker plugin".into());
                    }
                }
                index += 1;
            }
            _ if arg.starts_with(b"-Map=") => analysis.link_map = Some(arg[5..].to_vec()),
            _ if arg.starts_with(b"--Map=") => analysis.link_map = Some(arg[6..].to_vec()),
            _ if arg.starts_with(b"-o") => analysis.output = Some(arg[2..].to_vec()),
            _ => (),
        }
    }
    if verbose && analysis.inputs.is_empty() && analysis.output.is_none() {
        analysis.mode = Mode::Query;
    }
    if analysis.output.is_none() {
        analysis.output = Some(b"a.out".to_vec());
    }
    analysis
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn long_driver_options_and_language_reset() {
        let a = analyze(
            &[
                "--language=c++",
                "first",
                "--language",
                "none",
                "second.cp",
                "-c",
                "--output=earlier.o",
                "--output",
                "wanted.o",
            ]
            .map(OsString::from),
            Path::new("."),
        );
        assert_eq!(a.inputs, vec![b"first".to_vec(), b"second.cp".to_vec()]);
        assert_eq!(a.output, Some(b"wanted.o".to_vec()));
        assert_eq!(a.input_language(0), Some(b"c++".as_slice()));
        assert_eq!(a.input_language(1), Some(b"c++".as_slice()));
    }
    #[test]
    fn linker_forwarding_preserves_map_operands_and_last_selection() {
        let a = analyze(
            &[
                "unit.c",
                "-Wl,--as-needed,-Map,first.map",
                "-Xlinker",
                "-Map",
                "-Xlinker",
                "caller map,one.txt",
            ]
            .map(OsString::from),
            Path::new("."),
        );
        assert_eq!(a.inputs, vec![b"unit.c"]);
        assert_eq!(a.link_map, Some(b"caller map,one.txt".to_vec()));
        assert!(a.incomplete.is_empty());
        let a = analyze(&["unit.c", "-Wl,-Map"].map(OsString::from), Path::new("."));
        assert!(!a.incomplete.is_empty());
    }
    #[test]
    fn split_dwarf_selection_is_ordered() {
        for (flags, expected) in [
            (vec!["-gsplit-dwarf"], true),
            (vec!["-gsplit-dwarf", "-gno-split-dwarf"], false),
            (vec!["-gsplit-dwarf", "-gsplit-dwarf=single"], false),
            (vec!["-gno-split-dwarf", "-gsplit-dwarf=split"], true),
        ] {
            let args = flags.into_iter().map(OsString::from).collect::<Vec<_>>();
            assert_eq!(analyze(&args, Path::new(".")).split_dwarf, expected);
        }
    }
    #[test]
    fn response_bytes_and_empty_arguments() {
        let words = tokenize(b"'' 'a b' c\\ d \"x\\\"y\" \xff").unwrap();
        assert_eq!(
            words.iter().map(|s| s.as_bytes()).collect::<Vec<_>>(),
            vec![b"".as_slice(), b"a b", b"c d", b"x\"y", b"\xff"]
        );
        assert!(tokenize(b"'open").is_err());
    }
    #[test]
    fn legacy_clang_response_quoting() {
        let words = tokenize_dialect(
            br#"-Wl\,-Map\,unit.map "back\slash.c" "quote\"source.c" "paired\\back.c""#,
            true,
        )
        .unwrap();
        assert_eq!(
            words.iter().map(|s| s.as_bytes()).collect::<Vec<_>>(),
            vec![
                br"-Wl\,-Map\,unit.map".as_slice(),
                br"back\slash.c",
                b"quote\"source.c",
                br"paired\back.c"
            ]
        );
    }
    #[test]
    fn operands_are_not_source_inputs() {
        let args = [
            "-c",
            "-isystem",
            "include dir",
            "-I",
            "inc",
            "-MF",
            "dep",
            "-oobj",
            "a.c",
        ];
        let a = analyze(&args.map(OsString::from), Path::new("."));
        assert_eq!(a.mode, Mode::Compile);
        assert_eq!(a.inputs, vec![b"a.c"]);
        assert_eq!(a.output, Some(b"obj".to_vec()));
    }
    #[test]
    fn languages_apply_only_to_following_inputs() {
        let a = analyze(
            &["-x", "cpp-output", "first", "-xnone", "second.cpp", "-c"].map(OsString::from),
            Path::new("."),
        );
        assert_eq!(a.input_language(0), Some(b"cpp-output".as_slice()));
        assert_eq!(a.input_language(1), Some(b"c++".as_slice()));
        assert!(!a.preprocessed_inputs());
        let a = analyze(
            &["-x", "cpp-output", "first", "-c"].map(OsString::from),
            Path::new("."),
        );
        assert!(a.preprocessed_inputs());
    }
    #[test]
    fn explicit_language_overrides_the_cxx_driver_mode() {
        let a = analyze(
            &["-x", "c", "-c", "unit.c"].map(OsString::from),
            Path::new("."),
        );
        assert!(!a.cxx_inputs(true));
        let a = analyze(
            &["-x", "none", "-c", "unit.c"].map(OsString::from),
            Path::new("."),
        );
        assert!(a.cxx_inputs(true));
        let a = analyze(
            &["-x", "c++", "-c", "unit.c"].map(OsString::from),
            Path::new("."),
        );
        assert!(a.cxx_inputs(false));
    }
    #[test]
    fn delimiter_and_explicit_language() {
        let a = analyze(
            &["-x", "c++", "-S", "--", "-source"].map(OsString::from),
            Path::new("."),
        );
        assert_eq!(a.mode, Mode::Assembly);
        assert_eq!(a.language, Some(b"c++".to_vec()));
        assert_eq!(a.inputs, vec![b"-source"]);
    }
    #[test]
    fn mode_precedence_and_lto_disable() {
        let a = analyze(
            &["-E", "-c", "-flto", "-fno-lto", "unit.c"].map(OsString::from),
            Path::new("."),
        );
        assert_eq!(a.mode, Mode::Preprocess);
        assert!(!a.lto);
    }
    #[test]
    fn nix_wrapper_operands_are_not_inputs() {
        let a = analyze(
            &[
                "--param",
                "ssp-buffer-size=4",
                "-cxx-isystem",
                "/include/c++",
                "-D",
                "NAME=1",
                "-c",
                "unit.c",
            ]
            .map(OsString::from),
            Path::new("."),
        );
        assert_eq!(a.inputs, vec![b"unit.c"]);
        assert_eq!(a.mode, Mode::Compile);
    }
}
