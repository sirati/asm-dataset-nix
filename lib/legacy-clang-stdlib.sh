# Called after Nix argument assembly. Do not add preprocessing options to
# version queries, frontend entry points, or links containing only objects.
datasetLegacySource=0
datasetLegacyOperand=0
datasetLegacyLanguage=0
datasetLegacyPositional=0
for datasetLegacyArg in ${params+"${params[@]}"} ${extraAfter+"${extraAfter[@]}"}; do
    if [[ "$datasetLegacyOperand" = 1 ]]; then
        datasetLegacyOperand=0
        continue
    fi
    if [[ "$datasetLegacyLanguage" = 1 ]]; then
        datasetLegacyLanguage=0
        case "$datasetLegacyArg" in none|assembler) ;; *) datasetLegacySource=1;; esac
        continue
    fi
    if [[ "$datasetLegacyPositional" != 1 ]]; then
        case "$datasetLegacyArg" in
            --) datasetLegacyPositional=1; continue;;
            -x|--language) datasetLegacyLanguage=1; continue;;
            -xnone|-xassembler|--language=none|--language=assembler) continue;;
            -x?*|--language=*) datasetLegacySource=1; continue;;
            -o|--output|-I|-L|-B|-D|-U|-MF|-MT|-MQ|-isystem|-idirafter|-include|-imacros|-Xclang|-Xlinker|-Xassembler|-Xpreprocessor)
                datasetLegacyOperand=1; continue;;
            -?*) continue;;
        esac
    fi
    case "$datasetLegacyArg" in
        -|*.c|*.C|*.cc|*.cp|*.cpp|*.CPP|*.cxx|*.c++|*.h|*.hh|*.hpp|*.hxx|*.i|*.ii|*.S|*.sx)
            datasetLegacySource=1;;
    esac
done
if [[ "${cc1:-0}" != 1 && "$datasetLegacySource" = 1 ]]; then
    if [[ "${datasetLegacyArmEhabi:-0}" = 1 ]]; then
        extraBefore=(-mllvm -arm-enable-ehabi -mllvm -arm-enable-ehabi-descriptors ${extraBefore+"${extraBefore[@]}"})
    fi
    if [[ "${datasetLegacyPreferPic:-0}" = 1 ]]; then
        extraBefore=(-fPIC ${extraBefore+"${extraBefore[@]}"})
    fi
    extraBefore=(-include "$datasetLegacyStdlibHeader" ${extraBefore+"${extraBefore[@]}"})
fi
