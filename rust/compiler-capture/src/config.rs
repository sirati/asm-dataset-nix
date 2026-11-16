use serde::{Deserialize, Serialize};
use std::{
    fs, io,
    path::{Path, PathBuf},
};

pub const SCHEMA: u32 = 1;
pub const WRAPPER_REVISION: u32 = 1;

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Config {
    pub schema: u32,
    pub wrapper_revision: u32,
    pub executable: PathBuf,
    pub family: String,
    pub version: String,
    pub target: String,
    pub driver: String,
    pub backend: String,
    pub supervisor: PathBuf,
    #[serde(default = "preserve_debug")]
    pub debug_policy: String,
    #[serde(default)]
    pub nix_response_default: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub wrapper: Option<PathBuf>,
    #[serde(skip)]
    pub assembler_search: Option<PathBuf>,
}

fn preserve_debug() -> String {
    "preserve".into()
}

impl Config {
    pub fn load(path: &Path) -> io::Result<Self> {
        let file = fs::File::open(path)?;
        if file.metadata()?.len() > 65536 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "configuration exceeds 64 KiB",
            ));
        }
        let mut config: Self = serde_json::from_reader(file)?;
        if config.schema != SCHEMA
            || config.wrapper_revision != WRAPPER_REVISION
            || !config.executable.is_absolute()
            || !config.supervisor.is_absolute()
            || !["gcc", "clang", "assembler", "linker"].contains(&config.family.as_str())
            || ![
                "forward",
                "observe",
                "gcc-dumpdir",
                "gcc-trace",
                "clang-trace",
                "tool-observe",
            ]
            .contains(&config.backend.as_str())
            || config.version.is_empty()
            || config.target.is_empty()
            || config.driver.is_empty()
            || !["preserve", "required"].contains(&config.debug_policy.as_str())
            || config
                .wrapper
                .as_ref()
                .is_some_and(|path| !path.is_absolute())
            || config.backend.starts_with("gcc-") && config.family != "gcc"
            || config.backend.starts_with("clang-") && config.family != "clang"
            || config.backend == "tool-observe"
                && !["assembler", "linker"].contains(&config.family.as_str())
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "invalid compiler capture configuration",
            ));
        }
        if config.family == "clang" && config.version.starts_with("3.8.") {
            if let Some(wrapper) = &config.wrapper {
                use std::{io::Read, os::unix::fs::OpenOptionsExt};
                match fs::OpenOptions::new()
                    .read(true)
                    .custom_flags(libc::O_NOFOLLOW | libc::O_NONBLOCK)
                    .open(wrapper.join("nix-support/capture-assembler-search"))
                {
                    Ok(file) => {
                        if !file.metadata()?.is_file() || file.metadata()?.len() > 4096 {
                            return Err(io::Error::other("invalid assembler search metadata"));
                        }
                        let mut value = String::new();
                        file.take(4097).read_to_string(&mut value)?;
                        if value.len() > 4096 {
                            return Err(io::Error::other(
                                "assembler search metadata exceeds its limit",
                            ));
                        }
                        let path = PathBuf::from(value.trim_end_matches('\n'));
                        if !path.is_absolute() {
                            return Err(io::Error::other(
                                "assembler search metadata must name an absolute path",
                            ));
                        }
                        config.assembler_search = Some(path);
                    }
                    Err(error) if error.kind() == io::ErrorKind::NotFound => (),
                    Err(error) => return Err(error),
                }
            }
        }
        Ok(config)
    }
}
