# Old Clang response readers retain unquoted backslashes before commas.
# Quote every argument and escape backslashes/quotes inside those quotes.
# printf treats the argument as data, including newlines and non-UTF-8 bytes.
_datasetLegacyClangResponse() {
    local argument
    for argument in "$@"; do
        argument=${argument//\\/\\\\}
        argument=${argument//\"/\\\"}
        printf '"%s"\n' "$argument"
    done
}
