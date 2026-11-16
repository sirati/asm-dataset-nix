# Replacing cc reconstructs stdenv. Negative reference constraints name old
# bootstrap outputs; their string contexts would fetch those forbidden outputs
# just to check their absence. Preserve the exact constraints without making
# them build inputs. Positive prerequisites keep their normal contexts.
{ pkgs }:
{ stdenv, cc }:
(pkgs.overrideCC stdenv cc).override (old:
  pkgs.lib.optionalAttrs (old ? disallowedRequisites) {
    disallowedRequisites = map (path: builtins.unsafeDiscardStringContext (toString path)) old.disallowedRequisites;
  })
