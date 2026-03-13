# Generate a JSON manifest for a specific slice of the matrix.
# Used by the generate-manifest app which calls nix eval on _meta.
#
# This module provides helpers for working with manifest data after
# it has been generated, e.g., augmenting with dependency info.

{ pkgs, lib }:

{
  # Script to augment a manifest JSON with nix-store dependency info
  augmentWithDepsScript = pkgs.writeShellScript "augment-manifest-deps" ''
    set -euo pipefail
    INPUT="''${1:?Usage: augment-manifest-deps <input.json> [output.json]}"
    OUTPUT="''${2:-''${INPUT%.json}-with-deps.json}"

    ${pkgs.python3}/bin/python3 -c "
import json, subprocess, sys

with open(sys.argv[1], 'r') as f:
    manifest = json.load(f)

# Walk the nested structure: pkg -> arch -> variant -> entry
total = 0
for pkg_name, archs in manifest.items():
    for arch_name, variants in archs.items():
        for variant_name, entry in variants.items():
            total += 1
            drv_path = entry.get('drvPath', '')
            if drv_path:
                try:
                    result = subprocess.run(
                        ['nix-store', '-q', '--references', drv_path],
                        capture_output=True, text=True, timeout=10
                    )
                    deps = [d for d in result.stdout.strip().split('\n') if d]
                    entry['buildDeps'] = deps
                except Exception as e:
                    entry['buildDeps'] = []
                    entry['depsError'] = str(e)

with open(sys.argv[2], 'w') as f:
    json.dump(manifest, f, indent=2)

print(f'Augmented manifest written to {sys.argv[2]}')
print(f'Total derivations: {total}')
" "$INPUT" "$OUTPUT"
  '';
}
