# Keep the unwrapped compiler interface available to package expressions while
# routing its driver aliases through capture. Resource/support trees stay links
# to the original compiler and no Nix argument assembly runs on this path.
{pkgs,captureTool}:
{cc,family,version,target,entries,wrapper,debugPolicy}:
pkgs.buildPackages.runCommand "capture-${cc.name}-raw-drivers" {
  nativeBuildInputs=[pkgs.buildPackages.python3];
  metadata=pkgs.writeText "capture-raw-driver-metadata.json" (builtins.toJSON {
    schema=1;wrapper_revision=1;inherit family version target;
    backend="${family}-trace";supervisor="${captureTool}/bin/compiler-capture";
    debug_policy=debugPolicy;nix_response_default=false;wrapper=toString wrapper;
  });
  executables=pkgs.writeText "capture-raw-driver-entrypoints.json" (builtins.toJSON (map (entry: entry.executable) entries));
} ''
  python3 - ${cc} "$out" "$metadata" "$executables" <<'PY'
  import json,os,pathlib,shlex,sys
  original,output,metadata,entrypoints=map(pathlib.Path,sys.argv[1:])
  output.mkdir();(output/'bin').mkdir();(output/'nix-support').mkdir()
  config=json.loads(metadata.read_text())
  drivers={os.path.realpath(path) for path in json.loads(entrypoints.read_text())}
  for path in original.iterdir():
      if path.name in ['bin','nix-support']:continue
      (output/path.name).symlink_to(path)
  if (original/'nix-support').is_dir():
      for path in (original/'nix-support').iterdir():(output/'nix-support'/path.name).symlink_to(path)
  for path in (original/'bin').iterdir():
      destination=output/'bin'/path.name
      if os.path.realpath(path) not in drivers:
          destination.symlink_to(path);continue
      # Versioned C++ aliases still select the C++ driver when argv[0]
      # names the compiler-provided ++ entry. Keep that mode explicit.
      facts=dict(config,executable=str(path),driver='unwrapped-c++' if '++' in path.name else 'unwrapped-'+path.name)
      configuration=output/'nix-support'/('capture-raw-'+path.name+'.json')
      configuration.write_text(json.dumps(facts,separators=(',',':')))
      destination.write_text('#!${pkgs.stdenv.shell}\nexec '+shlex.quote(config['supervisor'])+' --config '+shlex.quote(str(configuration))+' -- "$@"\n')
      destination.chmod(0o755)
  PY
''
