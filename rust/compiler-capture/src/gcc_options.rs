//! GCC quotes COLLECT_GCC_OPTIONS with single quotes, escaping an embedded
//! quote outside those quoted spans. It is data, never a shell command.
use std::io;

fn parse(value: &[u8]) -> io::Result<Vec<Vec<u8>>> {
    let mut result = Vec::new();
    let mut word = Vec::new();
    let mut quoted = false;
    let mut escaped = false;
    let mut started = false;
    for &byte in value {
        if escaped {
            word.push(byte);
            escaped = false;
            started = true;
        } else if byte == b'\\' && !quoted {
            escaped = true;
            started = true;
        } else if byte == b'\'' {
            quoted = !quoted;
            started = true;
        } else if byte.is_ascii_whitespace() && !quoted {
            if started {
                result.push(std::mem::take(&mut word));
                started = false;
            }
        } else {
            word.push(byte);
            started = true;
        }
    }
    if quoted || escaped {
        return Err(io::Error::other("malformed GCC option environment"));
    }
    if started {
        result.push(word);
    }
    Ok(result)
}

pub fn auxiliary_output(value: &[u8], output: &[u8]) -> io::Result<Vec<u8>> {
    let mut arguments = parse(value)?;
    let mut found = false;
    let mut index = 0;
    while index < arguments.len() {
        if arguments[index] == b"-dumpdir" {
            if index + 1 == arguments.len() {
                return Err(io::Error::other("GCC dumpdir has no operand"));
            }
            arguments[index + 1] = output.to_vec();
            index += 1;
        }
        if arguments[index] == b"-o" {
            if index + 1 == arguments.len() {
                return Err(io::Error::other("GCC output option has no operand"));
            }
            arguments[index + 1] = output.to_vec();
            found = true;
            index += 1;
        }
        index += 1;
    }
    if !found {
        arguments.extend([b"-o".to_vec(), output.to_vec()]);
    }
    Ok(encode(arguments))
}

// GCC 15's plugin decodes the first -dumpdir with quote counts extending to
// the last one. Driver-only links can put the same option in this environment
// twice. Retain the effective last operand so the plugin reads the intended
// directory instead of treating intervening flags as part of the filename.
pub fn deduplicate_dumpdir(value: &[u8]) -> io::Result<Vec<u8>> {
    let arguments = parse(value)?;
    let last = arguments.iter().rposition(|arg| arg == b"-dumpdir");
    let mut result = Vec::new();
    let mut index = 0;
    while index < arguments.len() {
        if arguments[index] == b"-dumpdir" {
            if index + 1 == arguments.len() {
                return Err(io::Error::other("GCC dumpdir has no operand"));
            }
            if Some(index) != last {
                index += 2;
                continue;
            }
        }
        result.push(arguments[index].clone());
        index += 1;
    }
    Ok(encode(result))
}

fn encode(arguments: Vec<Vec<u8>>) -> Vec<u8> {
    let mut encoded = Vec::new();
    for argument in arguments {
        if !encoded.is_empty() {
            encoded.push(b' ');
        }
        encoded.push(b'\'');
        for byte in argument {
            if byte == b'\'' {
                encoded.extend_from_slice(b"'\\''");
            } else {
                encoded.push(byte);
            }
        }
        encoded.push(b'\'');
    }
    encoded
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn preserves_quoted_bytes_and_changes_only_the_auxiliary_output() {
        let original = b"'-O2' '-o' 'a b' '-DSTR=one\\two' '-DQUOTE=a'\\''b' '\xff'";
        let updated = auxiliary_output(original, b"/capture/view/lto").unwrap();
        assert_eq!(
            parse(&updated).unwrap(),
            vec![
                b"-O2".to_vec(),
                b"-o".to_vec(),
                b"/capture/view/lto".to_vec(),
                b"-DSTR=one\\two".to_vec(),
                b"-DQUOTE=a'b".to_vec(),
                vec![255]
            ]
        );
        assert!(auxiliary_output(b"'unterminated", b"out").is_err());
    }
    #[test]
    fn retains_only_the_effective_dumpdir() {
        let value = b"'-dumpdir' 'first' '-g' '-DMSG=two words' '-dumpdir' 'last'";
        assert_eq!(
            parse(&deduplicate_dumpdir(value).unwrap()).unwrap(),
            vec![
                b"-g".to_vec(),
                b"-DMSG=two words".to_vec(),
                b"-dumpdir".to_vec(),
                b"last".to_vec()
            ]
        );
    }
    #[test]
    fn coordinator_redirects_a_driver_auxiliary_prefix() {
        assert_eq!(
            parse(
                &auxiliary_output(
                    b"'-gsplit-dwarf' '-dumpdir' 'program-' '-o' 'program'",
                    b"/capture/gcc-lto"
                )
                .unwrap()
            )
            .unwrap(),
            vec![
                b"-gsplit-dwarf".to_vec(),
                b"-dumpdir".to_vec(),
                b"/capture/gcc-lto".to_vec(),
                b"-o".to_vec(),
                b"/capture/gcc-lto".to_vec()
            ]
        );
    }
}
