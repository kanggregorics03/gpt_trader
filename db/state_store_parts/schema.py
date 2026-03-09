from db.state_store_parts.schema_init import StateStoreSchemaInitMixin
from db.state_store_parts.schema_migration import StateStoreSchemaMigrationMixin


class StateStoreSchemaMixin(
    StateStoreSchemaInitMixin,
    StateStoreSchemaMigrationMixin,
):
    pass
