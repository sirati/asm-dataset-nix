# Hash evaluation inputs, not the complete checkout. The runner image omits
# docs and Git metadata, so its token must match the submitter's checkout.
{ lib, system, root, inputs ? {} }:
let
  collect = relative: pythonOnly:
    let entries = builtins.readDir (root + "/${relative}");
    in lib.concatMap (name:
      let path = "${relative}/${name}"; type = entries.${name};
      in if type == "directory" && !(builtins.elem name [ "tests" "__pycache__" ".pytest_cache" ]) then collect path pythonOnly
         else if type == "regular" && (!pythonOnly || lib.hasSuffix ".py" name)
         then [path] else []
    ) (builtins.attrNames entries);
  files = [ "flake.nix" "flake.lock" "table.md" ] ++ collect "lib" false
    ++ collect "python/compiler_suit_runner" true;
in builtins.hashString "sha256" (builtins.toJSON {
  schema = 1;
  inherit system inputs;
  files = builtins.listToAttrs (map (path: { name = path; value = builtins.hashFile "sha256" (root + "/${path}"); }) files);
})
