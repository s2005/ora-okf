"""Shared fixtures for the unit test suite.

``build_sample_model`` is exposed both as the module-level function tests can
call directly (when they need to tweak or inspect the model before use) and as
the ``sample_model`` fixture, so a test can pick whichever style reads better.
"""

from __future__ import annotations

import pytest

from ora_okf.model import (
    OBJECT_TYPE_PACKAGE,
    OBJECT_TYPE_TRIGGER,
    Column,
    Constraint,
    ConstraintColumn,
    DbLink,
    Index,
    IndexColumn,
    Job,
    MaterializedView,
    MViewLog,
    ObjectType,
    Program,
    SampleData,
    SchemaModel,
    Sequence,
    Synonym,
    Table,
    TypeAttribute,
    View,
)

SCHEMA_NAME = "APP_OWNER"
FOREIGN_SCHEMA = "OTHER_SCHEMA"
GENERATED_AT = "2026-01-01 00:00:00 UTC"


def build_sample_model() -> SchemaModel:
    """Build a small but broad :class:`SchemaModel` covering every object kind."""
    orders = Table(
        name="ORDERS",
        comment="Customer orders.",
        columns=(
            Column(name="ID", data_type="NUMBER(10)", nullable=False, position=1),
            Column(name="CUSTOMER_ID", data_type="NUMBER(10)", nullable=False, position=2),
            Column(name="STATUS", data_type="VARCHAR2(20)", nullable=False, position=3),
            Column(
                name="AMOUNT",
                data_type="NUMBER(10,2)",
                nullable=True,
                comment="Order amount.",
                position=4,
            ),
        ),
        constraints=(
            Constraint(
                name="ORDERS_PK",
                constraint_type="P",
                table_name="ORDERS",
                columns=(ConstraintColumn(name="ID", position=1),),
            ),
            Constraint(
                name="ORDERS_CUST_FK",
                constraint_type="R",
                table_name="ORDERS",
                columns=(ConstraintColumn(name="CUSTOMER_ID", position=1),),
                referenced_owner=SCHEMA_NAME,
                referenced_table="CUSTOMERS",
                referenced_columns=("ID",),
                delete_rule="CASCADE",
            ),
            Constraint(
                name="ORDERS_AMOUNT_CK",
                constraint_type="C",
                table_name="ORDERS",
                search_condition="AMOUNT >= 0",
            ),
        ),
        indexes=(
            Index(
                name="ORDERS_STATUS_IX",
                table_name="ORDERS",
                unique=False,
                index_type="NORMAL",
                columns=(IndexColumn(name="STATUS", position=1),),
            ),
        ),
        row_count=2,
        sample=SampleData(columns=("ID", "STATUS"), rows=(("1", "NEW"), ("2", "SHIPPED"))),
    )

    customers = Table(
        name="CUSTOMERS",
        columns=(
            Column(name="ID", data_type="NUMBER(10)", nullable=False, position=1),
            Column(name="NAME", data_type="VARCHAR2(100)", nullable=True, position=2),
        ),
        is_global_temporary=True,
        on_commit="ON COMMIT DELETE ROWS",
        duration="SYS$TRANSACTION",
    )

    order_summary = View(
        name="ORDER_SUMMARY",
        comment="Summary view.",
        definition="SELECT id, status FROM orders",
        columns=(
            Column(name="ID", data_type="NUMBER(10)", nullable=False, position=1),
            Column(name="STATUS", data_type="VARCHAR2(20)", nullable=False, position=2),
        ),
    )

    orders_seq = Sequence(
        name="ORDERS_SEQ",
        min_value="1",
        max_value="999999999999999999999999999",
        increment_by="1",
        cache_size="20",
        cycle=False,
        ordered=False,
    )

    order_pkg = Program(
        name="ORDER_PKG",
        program_type=OBJECT_TYPE_PACKAGE,
        spec_source="PACKAGE ORDER_PKG IS\n  PROCEDURE PLACE;\nEND ORDER_PKG;",
        body_source="PACKAGE BODY ORDER_PKG IS\n  PROCEDURE PLACE IS BEGIN NULL; END;\nEND ORDER_PKG;",
        status="VALID",
    )

    orders_trg = Program(
        name="ORDERS_TRG",
        program_type=OBJECT_TYPE_TRIGGER,
        source="TRIGGER ORDERS_TRG BEFORE INSERT ON ORDERS FOR EACH ROW BEGIN NULL; END;",
        status="VALID",
        table_name="ORDERS",
        triggering_event="INSERT",
        trigger_type="BEFORE EACH ROW",
    )

    address_type = ObjectType(
        name="ADDRESS_TYPE",
        typecode="OBJECT",
        attributes=(
            TypeAttribute(name="STREET", data_type="VARCHAR2(100)"),
            TypeAttribute(name="CITY", data_type="VARCHAR2(50)"),
        ),
    )

    cust_syn = Synonym(name="CUST_SYN", target_owner=FOREIGN_SCHEMA, target_name="CUSTOMERS")

    remote_link = DbLink(name="REMOTE_LINK", username="REMOTE_USER", host="remote-host")

    nightly_job = Job(
        name="NIGHTLY_JOB",
        job_type="PLSQL_BLOCK",
        job_action="BEGIN NULL; END;",
        schedule_type="CALENDAR",
        repeat_interval="FREQ=DAILY",
        enabled=True,
        state="SCHEDULED",
        job_class="DEFAULT_JOB_CLASS",
        comments="Nightly job.",
    )

    sales_mv = MaterializedView(
        name="SALES_MV",
        query="SELECT * FROM ORDERS",
        refresh_method="FAST",
        refresh_mode="ON DEMAND",
        build_mode="IMMEDIATE",
        compile_state="VALID",
        master_tables=(f"{SCHEMA_NAME}.ORDERS",),
    )

    orders_mlog = MViewLog(
        name="ORDERS_MLOG",
        master_table="ORDERS",
        master_owner=SCHEMA_NAME,
        rowids=True,
        primary_key=True,
        sequence=False,
        include_new_values=True,
        filter_columns=("STATUS",),
    )

    return SchemaModel(
        schema_name=SCHEMA_NAME,
        generated_at=GENERATED_AT,
        database_version="Oracle Database 19c",
        tables=(orders, customers),
        views=(order_summary,),
        sequences=(orders_seq,),
        programs=(order_pkg, orders_trg),
        types=(address_type,),
        synonyms=(cust_syn,),
        db_links=(remote_link,),
        jobs=(nightly_job,),
        mviews=(sales_mv,),
        mview_logs=(orders_mlog,),
        referenced_schemas=(FOREIGN_SCHEMA,),
    )


@pytest.fixture
def sample_model() -> SchemaModel:
    """Return a fresh, broad :class:`SchemaModel` for rendering/writer tests."""
    return build_sample_model()
