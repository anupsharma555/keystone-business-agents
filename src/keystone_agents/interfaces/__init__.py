"""Provider-neutral integration interfaces."""

from keystone_agents.interfaces.table_mirror import (
    AirtableMirrorProvider,
    DryRunTableMirrorProvider,
    GoogleSheetsMirrorProvider,
    LocalTableMirrorCRMProvider,
    TableMirrorProvider,
    build_crm_provider,
    build_local_crm_provider_from_table_records,
    build_table_mirror_provider,
)

__all__ = [
    "AirtableMirrorProvider",
    "DryRunTableMirrorProvider",
    "GoogleSheetsMirrorProvider",
    "LocalTableMirrorCRMProvider",
    "TableMirrorProvider",
    "build_crm_provider",
    "build_local_crm_provider_from_table_records",
    "build_table_mirror_provider",
]
