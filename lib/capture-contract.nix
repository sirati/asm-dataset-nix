# Compiler manifests stay at schema 1; publication revision 2 adds independently
# selected provenance and verified installed-file debug information.
{ family, policy ? "required", debugPolicy ? "required" }:
{
  schema = 1;
  wrapper_revision = 1;
  inherit policy;
  debug_policy = debugPolicy;
  backend = "${family}-trace";
  required_elf_associations = true;
  required_wrapper_identity = true;
  publication_schema = 2;
  provenance_schema = 1;
  separate_debug_info = true;
}
