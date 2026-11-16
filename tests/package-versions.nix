# Pure catalog and resolver checks. No compiler instantiation or builds.
{ lib }:
let
  providers = {
    current = { revision = "fixture-current"; narHash = "fixture-content"; };
  };
  packages = [ { attr = "hello"; label = "hello"; } ];
  make = args: import ../lib/package-versions.nix ({ inherit lib providers packages; releases = []; } // args);
  base = (make {}).byId.hello;
  entry = id: version: base // {
    packageId = id; label = id; isDefault = false; expectedVersion = version;
    releaseId = "fixture-release-${id}";
  };
  rejects = value: !(builtins.tryEval (builtins.deepSeq value true)).success;
  versions = [ "2.1" "2+1" "2_1" "2-1" ];
  extras = lib.imap0 (i: version: entry "hello--r${toString i}" version) versions;
  catalog = make { releases = extras; };
in assert rejects (make { releases = [base]; });
assert rejects (make { packages = packages ++ packages; });
assert rejects (make { packages = [{ attr = "hello"; label = "bad--family"; }]; });
assert rejects (make { releases = [(entry "bad.path" "1")]; });
assert rejects (make { releases = [(entry "hello__unsafe" "1")]; });
assert catalog.defaultIds == ["hello"];
assert map (p: p.expectedVersion) (builtins.filter (p: !p.isDefault) catalog.entries) == versions;
assert builtins.attrNames (make { packages = [{attr = throw "forced package recipe"; label = "lazy";}]; }).byId == ["lazy"];
{
  passed = true;
  checks = [ "duplicate ids" "duplicate families" "reserved delimiters" "unsafe keys"
    "exact versions" "default aliases" "lazy enumeration" ];
}
