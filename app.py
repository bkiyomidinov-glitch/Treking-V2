from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
import math
from typing import Any, Generator, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError, model_validator


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "supply_chain_tj.sqlite3"
STATIC_DIR = BASE_DIR / "static"
SCHEMA_VERSION = 3
STAGE_ORDER = {"factory": 0, "workshop": 1, "warehouse": 2, "transport": 3}
STAGE_TITLES = {
    "factory": "Корхона",
    "workshop": "Сех ва назорати дохилӣ",
    "warehouse": "Анбор ва феҳрист",
    "transport": "Нақлиёт",
}


@contextmanager
def connect() -> Generator[sqlite3.Connection, None, None]:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def insert_event(
    db: sqlite3.Connection,
    product_id: int,
    stage: str,
    location_id: int,
    responsible_id: int,
    operator_id: int,
    metadata: dict[str, Any],
) -> sqlite3.Row:
    previous = db.execute(
        "SELECT event_hash FROM trace_events WHERE product_id=? ORDER BY sequence DESC LIMIT 1",
        (product_id,),
    ).fetchone()
    sequence = db.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM trace_events WHERE product_id=?",
        (product_id,),
    ).fetchone()[0]
    timestamp = now()
    previous_hash = previous["event_hash"] if previous else "0" * 64
    entry = {
        "product_id": product_id,
        "sequence": sequence,
        "stage": stage,
        "location_id": location_id,
        "responsible_id": responsible_id,
        "operator_id": operator_id,
        "timestamp": timestamp,
        "metadata": metadata,
        "previous_hash": previous_hash,
    }
    event_hash = hashlib.sha256(canonical_json(entry).encode("utf-8")).hexdigest()
    cursor = db.execute(
        "INSERT INTO trace_events(product_id,sequence,stage,location_id,responsible_id,operator_id,timestamp,metadata,previous_hash,event_hash,schema_version) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            product_id,
            sequence,
            stage,
            location_id,
            responsible_id,
            operator_id,
            timestamp,
            canonical_json(metadata),
            previous_hash,
            event_hash,
            SCHEMA_VERSION,
        ),
    )
    return db.execute("SELECT * FROM trace_events WHERE id=?", (cursor.lastrowid,)).fetchone()


def add_revision(
    db: sqlite3.Connection,
    product_id: int,
    config: dict[str, Any],
    changed_by: int,
    note: str,
) -> int:
    version = db.execute(
        "SELECT COALESCE(MAX(version), 0) + 1 FROM product_revisions WHERE product_id=?",
        (product_id,),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO product_revisions(product_id,version,config,changed_by,note,created_at,schema_version) VALUES(?,?,?,?,?,?,?)",
        (product_id, version, canonical_json(config), changed_by, note, now(), SCHEMA_VERSION),
    )
    return version


def seed_database(db: sqlite3.Connection) -> None:
    locations = [
        ("Корхонаи Душанбе", "factory", "Душанбе", "хиёбони Рӯдакӣ, 118", "Душанбе"),
        ("Корхонаи Суғд", "factory", "Суғд", "кӯчаи Саноат, 24", "Хуҷанд"),
        ("Корхонаи Хатлон", "factory", "Хатлон", "хиёбони Исмоили Сомонӣ, 37", "Бохтар"),
        ("Анбори марказии Душанбе", "warehouse", "Душанбе", "ноҳияи Сино, кӯчаи Борбад, 42", "Душанбе"),
        ("Маркази тақсимоти Суғд", "warehouse", "Суғд", "шаҳраки Ғафуров, роҳи саноатӣ, 9", "Бӯстон"),
        ("Анбори Хатлон", "warehouse", "Хатлон", "кӯчаи Ҷалолиддини Балхӣ, 16", "Бохтар"),
        ("Анбори Помир", "warehouse", "ВМКБ", "хиёбони Шоҳтемур, 5", "Хоруғ"),
    ]
    db.executemany(
        "INSERT INTO locations(name,kind,region,address,city) VALUES(?,?,?,?,?)",
        locations,
    )

    staff = [
        ("Фарҳод Каримов", "Роҳбари корхона", "TJ-001", "+992 93 700 10 01"),
        ("Меҳрона Саидова", "Устои сех", "TJ-002", "+992 93 700 10 02"),
        ("Комрон Назаров", "Анбордор", "TJ-003", "+992 93 700 10 03"),
        ("Шаҳноза Раҳмонӣ", "Қабулкунандаи бор", "TJ-004", "+992 93 700 10 04"),
        ("Ҷамшед Раҳимов", "Ронанда", "TJ-005", "+992 93 700 10 05"),
        ("Манижа Ҳакимова", "Ронанда", "TJ-006", "+992 93 700 10 06"),
        ("Сомон Давлатов", "Логист", "TJ-007", "+992 93 700 10 07"),
        ("Зебо Нурова", "Оператори низом", "TJ-008", "+992 93 700 10 08"),
    ]
    db.executemany(
        "INSERT INTO staff(name,role,employee_no,phone) VALUES(?,?,?,?)",
        staff,
    )

    vehicles = [
        ("01TJ101AA", "Isuzu NPR", "Ҷамшед Раҳимов", "+992 93 700 10 05", 8, 1),
        ("02TJ202BB", "Hyundai Mighty", "Манижа Ҳакимова", "+992 93 700 10 06", 5, 1),
        ("03TJ303CC", "Mercedes-Benz Atego", "Саидҷон Холиқов", "+992 93 700 10 09", 12, 0),
    ]
    db.executemany(
        "INSERT INTO vehicles(plate,brand,driver,driver_phone,capacity,refrigerated) VALUES(?,?,?,?,?,?)",
        vehicles,
    )

    routes = [
        ("Душанбе — Хуҷанд", "Душанбе", "Хуҷанд", 305, 6),
        ("Душанбе — Бохтар", "Душанбе", "Бохтар", 100, 2),
        ("Душанбе — Хоруғ", "Душанбе", "Хоруғ", 525, 13),
    ]
    db.executemany(
        "INSERT INTO routes(name,origin,destination,distance_km,duration_hours) VALUES(?,?,?,?,?)",
        routes,
    )

    demo_products = [
        {
            "barcode": "4850000001001", "batch_id": "TJ-26-101", "name": "Маркер «Помир»",
            "config": {"type": "marker", "color": "#729c82", "capColor": "#d2a35e", "length": 12, "diameter": 1.8, "shape": "faceted"},
            "weight": 0.18, "qc_status": "Санҷида шуд", "note": "Нӯги борик, бастаи навбати якум", "stage": "factory",
        },
        {
            "barcode": "4850000001002", "batch_id": "TJ-26-102", "name": "Маркер «Суғд»",
            "config": {"type": "marker", "color": "#6e9eb5", "capColor": "#d66e62", "length": 13, "diameter": 2.0, "shape": "round"},
            "weight": 0.22, "qc_status": "Санҷида шуд", "note": "Сиёҳии кабуд, навбати дуюм", "stage": "workshop",
        },
        {
            "barcode": "4850000001003", "batch_id": "TJ-26-103", "name": "Маркер «Вахш»",
            "config": {"type": "marker", "color": "#d66e62", "capColor": "#e4c66d", "length": 11, "diameter": 1.6, "shape": "faceted"},
            "weight": 0.2, "qc_status": "Санҷида шуд", "note": "Маркер барои хаткашӣ", "stage": "warehouse",
        },
        {
            "barcode": "4850000001004", "batch_id": "TJ-26-104", "name": "Маркер «Хоруғ»",
            "config": {"type": "marker", "color": "#d5a74e", "capColor": "#769b83", "length": 14, "diameter": 2.2, "shape": "round"},
            "weight": 0.25, "qc_status": "Санҷида шуд", "note": "Бастаи махсус барои маркази Помир", "stage": "transport",
        },
    ]
    for index, item in enumerate(demo_products):
        cursor = db.execute(
            "INSERT INTO products(barcode,batch_id,name,config,config_version,weight,qc_status,note,current_stage,revision,created_at,schema_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                item["barcode"], item["batch_id"], item["name"], canonical_json(item["config"]), 1,
                item["weight"], item["qc_status"], item["note"], "factory", 1, now(), SCHEMA_VERSION,
            ),
        )
        product_id = cursor.lastrowid
        factory_id = index % 3 + 1
        add_revision(db, product_id, item["config"], 1, "Танзимоти ибтидоии корхона")
        insert_event(db, product_id, "factory", factory_id, 1, 8, {"batch_weight_kg": item["weight"], "quality": item["qc_status"], "note": item["note"]})

        if STAGE_ORDER[item["stage"]] >= STAGE_ORDER["workshop"]:
            insert_event(db, product_id, "workshop", factory_id, 2, 8, {"inspection": "Мувофиқи талабот", "line": f"Хати истеҳсолии {index + 1}", "adjustment": "Сифати сарпӯш санҷида шуд"})
        if STAGE_ORDER[item["stage"]] >= STAGE_ORDER["warehouse"]:
            warehouse_id = 4 + index % 4
            insert_event(db, product_id, "warehouse", warehouse_id, 3, 4, {"shelf": f"A-{10 + index}", "sku": f"TJ-MRK-{index + 101}", "price_tjs": 18 + index * 2, "temperature_c": 21, "package": "Солим"})
        if item["stage"] == "transport":
            insert_event(db, product_id, "transport", 7, 7, 8, {"vehicle": "01TJ101AA", "driver": "Ҷамшед Раҳимов", "driver_phone": "+992 93 700 10 05", "route": "Душанбе — Хоруғ", "distance_km": 525, "eta_hours": 13, "temperature_c": 17, "seal": "TJ-PL-104", "cargo_state": "Бе осеб"})
        db.execute("UPDATE products SET current_stage=? WHERE id=?", (item["stage"], product_id))


MODEL_SPECS: dict[str, list[dict[str, Any]]] = {
    "marker": [
        {"key": "length", "label": "Дарозӣ", "type": "range", "default": 12, "min": 8, "max": 16, "step": 0.5, "unit": "см"},
        {"key": "diameter", "label": "Ғафсӣ", "type": "range", "default": 1.8, "min": 1.2, "max": 2.8, "step": 0.1, "unit": "мм"},
        {"key": "shape", "label": "Шакл", "type": "select", "default": "round", "options": [{"value": "round", "label": "Даврашакл"}, {"value": "faceted", "label": "Фасетдор"}]},
    ],
    "pen": [
        {"key": "length_cm", "label": "Дарозӣ", "type": "range", "default": 14, "min": 10, "max": 20, "step": 0.5, "unit": "см"},
        {"key": "diameter_mm", "label": "Ғафсии корпус", "type": "range", "default": 1, "min": 0.6, "max": 1.6, "step": 0.1, "unit": "мм"},
        {"key": "retractable", "label": "Механизм", "type": "select", "default": "cap", "options": [{"value": "cap", "label": "Бо сарпӯш"}, {"value": "click", "label": "Тугмадор"}]},
    ],
    "pencil": [
        {"key": "length_cm", "label": "Дарозӣ", "type": "range", "default": 18, "min": 10, "max": 22, "step": 1, "unit": "см"},
        {"key": "diameter_mm", "label": "Ғафсӣ", "type": "range", "default": 7, "min": 5, "max": 10, "step": 0.5, "unit": "мм"},
        {"key": "shape", "label": "Шакли буриш", "type": "select", "default": "hexagonal", "options": [{"value": "hexagonal", "label": "Шашкунҷа"}, {"value": "round", "label": "Даврашакл"}, {"value": "triangular", "label": "Секунҷа"}]},
    ],
    "notebook": [
        {"key": "width_cm", "label": "Бар", "type": "range", "default": 15, "min": 8, "max": 24, "step": 0.5, "unit": "см"},
        {"key": "height_cm", "label": "Баландӣ", "type": "range", "default": 21, "min": 10, "max": 32, "step": 0.5, "unit": "см"},
        {"key": "pages", "label": "Шумораи саҳифа", "type": "number", "default": 48, "min": 12, "max": 400, "step": 12, "unit": "саҳифа"},
        {"key": "ruling", "label": "Навъи варақ", "type": "select", "default": "lined", "options": [{"value": "lined", "label": "Хатдор"}, {"value": "grid", "label": "Хоначадор"}, {"value": "plain", "label": "Сафед"}]},
    ],
    "folder": [
        {"key": "width_cm", "label": "Бар", "type": "range", "default": 24, "min": 10, "max": 35, "step": 1, "unit": "см"},
        {"key": "height_cm", "label": "Баландӣ", "type": "range", "default": 32, "min": 12, "max": 45, "step": 1, "unit": "см"},
        {"key": "thickness_mm", "label": "Ғафсии ҷузвадон", "type": "range", "default": 8, "min": 2, "max": 35, "step": 1, "unit": "мм"},
        {"key": "material", "label": "Мавод", "type": "select", "default": "cardboard", "options": [{"value": "cardboard", "label": "Картон"}, {"value": "plastic", "label": "Пластик"}]},
    ],
    "bottle": [
        {"key": "capacity_ml", "label": "Ғунҷоиш", "type": "range", "default": 1000, "min": 250, "max": 3000, "step": 50, "unit": "мл"},
        {"key": "height_cm", "label": "Баландӣ", "type": "range", "default": 25, "min": 10, "max": 45, "step": 1, "unit": "см"},
        {"key": "diameter_cm", "label": "Диаметри бадан", "type": "range", "default": 8, "min": 4, "max": 16, "step": 0.5, "unit": "см"},
        {"key": "material", "label": "Мавод", "type": "select", "default": "plastic", "options": [{"value": "plastic", "label": "Пластик"}, {"value": "glass", "label": "Шиша"}, {"value": "metal", "label": "Металл"}]},
    ],
    "box": [
        {"key": "width_cm", "label": "Бар", "type": "range", "default": 20, "min": 5, "max": 60, "step": 1, "unit": "см"},
        {"key": "length_cm", "label": "Дарозӣ", "type": "range", "default": 30, "min": 5, "max": 80, "step": 1, "unit": "см"},
        {"key": "height_cm", "label": "Баландӣ", "type": "range", "default": 10, "min": 3, "max": 50, "step": 1, "unit": "см"},
        {"key": "material", "label": "Мавод", "type": "select", "default": "cardboard", "options": [{"value": "cardboard", "label": "Картон"}, {"value": "corrugated", "label": "Картони мавҷдор"}, {"value": "plastic", "label": "Пластик"}]},
    ],
    "ruler": [
        {"key": "length_cm", "label": "Дарозӣ", "type": "range", "default": 30, "min": 10, "max": 50, "step": 1, "unit": "см"},
        {"key": "width_cm", "label": "Бар", "type": "range", "default": 3, "min": 1, "max": 8, "step": 0.5, "unit": "см"},
        {"key": "material", "label": "Мавод", "type": "select", "default": "plastic", "options": [{"value": "plastic", "label": "Пластик"}, {"value": "wood", "label": "Чӯб"}, {"value": "metal", "label": "Металл"}]},
    ],
    "eraser": [
        {"key": "length_cm", "label": "Дарозӣ", "type": "range", "default": 5, "min": 2, "max": 12, "step": 0.5, "unit": "см"},
        {"key": "width_cm", "label": "Бар", "type": "range", "default": 2, "min": 1, "max": 6, "step": 0.5, "unit": "см"},
        {"key": "thickness_cm", "label": "Баландӣ", "type": "range", "default": 1, "min": 0.4, "max": 3, "step": 0.1, "unit": "см"},
        {"key": "hardness", "label": "Сахтӣ", "type": "select", "default": "medium", "options": [{"value": "soft", "label": "Нарм"}, {"value": "medium", "label": "Миёна"}, {"value": "hard", "label": "Сахт"}]},
    ],
    "mug": [
        {"key": "capacity_ml", "label": "Ғунҷоиш", "type": "range", "default": 350, "min": 100, "max": 1000, "step": 25, "unit": "мл"},
        {"key": "height_cm", "label": "Баландӣ", "type": "range", "default": 10, "min": 5, "max": 20, "step": 0.5, "unit": "см"},
        {"key": "diameter_cm", "label": "Диаметр", "type": "range", "default": 8, "min": 4, "max": 15, "step": 0.5, "unit": "см"},
        {"key": "material", "label": "Мавод", "type": "select", "default": "ceramic", "options": [{"value": "ceramic", "label": "Сафол"}, {"value": "glass", "label": "Шиша"}, {"value": "metal", "label": "Металл"}]},
    ],
    "soap": [
        {"key": "length_cm", "label": "Дарозӣ", "type": "range", "default": 9, "min": 4, "max": 16, "step": 0.5, "unit": "см"},
        {"key": "width_cm", "label": "Бар", "type": "range", "default": 6, "min": 3, "max": 12, "step": 0.5, "unit": "см"},
        {"key": "thickness_cm", "label": "Ғафсӣ", "type": "range", "default": 3, "min": 1, "max": 7, "step": 0.5, "unit": "см"},
        {"key": "scent", "label": "Бӯй", "type": "select", "default": "neutral", "options": [{"value": "neutral", "label": "Бетараф"}, {"value": "floral", "label": "Гул"}, {"value": "citrus", "label": "Ситрус"}]},
    ],
}

PRODUCT_CATALOG_SEED = [
    ("TJ-MRK-001", "Маркер «Помир»", "Канселярӣ", "дона", "marker", "Маркери канселярӣ"),
    ("TJ-MRK-002", "Маркер «Суғд»", "Канселярӣ", "дона", "marker", "Маркер барои рангкунӣ"),
    ("TJ-PEN-001", "Қалами шарикдор", "Канселярӣ", "дона", "pen", "Қалами сиёҳнавис"),
    ("TJ-PEN-002", "Қалами гелӣ", "Канселярӣ", "дона", "pen", "Қалами гелӣ"),
    ("TJ-PCL-001", "Қалами одӣ", "Канселярӣ", "дона", "pencil", "Қалами графитӣ"),
    ("TJ-PCL-002", "Қалами ранга", "Канселярӣ", "дона", "pencil", "Қалами ранга"),
    ("TJ-NBK-001", "Дафтари хатдор", "Канселярӣ", "дона", "notebook", "Дафтари хатдор"),
    ("TJ-NBK-002", "Дафтари хоначадор", "Канселярӣ", "дона", "notebook", "Дафтари хоначадор"),
    ("TJ-FLD-001", "Ҷузвадони ҳуҷҷат", "Канселярӣ", "дона", "folder", "Ҷузвадони пластикӣ барои ҳуҷҷат"),
    ("TJ-RUL-001", "Хаткашак", "Канселярӣ", "дона", "ruler", "Хаткашаки ченкунӣ"),
    ("TJ-ERS-001", "Поккунак", "Канселярӣ", "дона", "eraser", "Поккунаки нарм"),
    ("TJ-BTL-001", "Шишаи об", "Зарфҳо", "дона", "bottle", "Шишаи бисёркаратаи об"),
    ("TJ-BTL-002", "Термос", "Зарфҳо", "дона", "bottle", "Термоси нигоҳдорандаи гармӣ"),
    ("TJ-MUG-001", "Пиёла", "Зарфҳо", "дона", "mug", "Пиёлаи сафолӣ бо дастак"),
    ("TJ-BOX-001", "Қуттии бастабандӣ", "Бастабандӣ", "дона", "box", "Қуттии картонии бастабандӣ"),
    ("TJ-SOAP-001", "Собун", "Маҳсулоти рӯзгор", "дона", "soap", "Собуни сахти хушбӯй"),
]


def catalog_specifications(model: str) -> list[dict[str, Any]]:
    return [dict(specification) for specification in MODEL_SPECS[model]]


def attribute_specification(attribute: CatalogAttributeInput) -> dict[str, Any]:
    specification: dict[str, Any] = {
        "key": attribute.key,
        "label": attribute.label,
        "type": attribute.type,
        "default": attribute.default,
        "unit": attribute.unit,
    }
    if attribute.type == "number":
        specification.update(
            min=attribute.min_value,
            max=attribute.max_value,
            step=attribute.step,
        )
    elif attribute.type == "select":
        specification["options"] = [
            {"value": option, "label": option} for option in attribute.options
        ]
    return specification


def seed_product_catalog(db: sqlite3.Connection) -> None:
    products = [
        (*product, canonical_json(catalog_specifications(product[4])))
        for product in PRODUCT_CATALOG_SEED
    ]
    db.executemany(
        "INSERT OR IGNORE INTO product_catalog(sku,name,category,unit,model,description,specifications) VALUES(?,?,?,?,?,?,?)",
        products,
    )


def initialize_database() -> None:
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                kind TEXT NOT NULL, region TEXT NOT NULL, address TEXT NOT NULL,
                city TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS staff (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                role TEXT NOT NULL, employee_no TEXT NOT NULL, phone TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS vehicles (
                id INTEGER PRIMARY KEY AUTOINCREMENT, plate TEXT NOT NULL,
                brand TEXT NOT NULL, driver TEXT NOT NULL, driver_phone TEXT NOT NULL,
                capacity REAL NOT NULL, refrigerated INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS routes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                origin TEXT NOT NULL, destination TEXT NOT NULL,
                distance_km REAL NOT NULL, duration_hours REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS product_catalog (
                id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL, category TEXT NOT NULL, unit TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT 'box', description TEXT NOT NULL DEFAULT '',
                specifications TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT, barcode TEXT UNIQUE NOT NULL,
                batch_id TEXT NOT NULL, name TEXT NOT NULL, config TEXT NOT NULL,
                config_version INTEGER NOT NULL, weight REAL NOT NULL,
                qc_status TEXT NOT NULL, note TEXT NOT NULL,
                current_stage TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL, schema_version INTEGER NOT NULL,
                catalog_id INTEGER REFERENCES product_catalog(id)
            );
            CREATE TABLE IF NOT EXISTS product_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
                version INTEGER NOT NULL, config TEXT NOT NULL, changed_by INTEGER NOT NULL,
                note TEXT NOT NULL, created_at TEXT NOT NULL, schema_version INTEGER NOT NULL,
                UNIQUE(product_id, version),
                FOREIGN KEY(product_id) REFERENCES products(id),
                FOREIGN KEY(changed_by) REFERENCES staff(id)
            );
            CREATE TABLE IF NOT EXISTS trace_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER NOT NULL,
                sequence INTEGER NOT NULL, stage TEXT NOT NULL, location_id INTEGER NOT NULL,
                responsible_id INTEGER NOT NULL, operator_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL, metadata TEXT NOT NULL,
                previous_hash TEXT NOT NULL, event_hash TEXT NOT NULL,
                schema_version INTEGER NOT NULL, UNIQUE(product_id, sequence),
                FOREIGN KEY(product_id) REFERENCES products(id),
                FOREIGN KEY(location_id) REFERENCES locations(id),
                FOREIGN KEY(responsible_id) REFERENCES staff(id),
                FOREIGN KEY(operator_id) REFERENCES staff(id)
            );
            CREATE TRIGGER IF NOT EXISTS trace_events_no_update
            BEFORE UPDATE ON trace_events BEGIN
                SELECT RAISE(ABORT, 'Рӯйдоди аудитро тағйир додан мумкин нест');
            END;
            CREATE TRIGGER IF NOT EXISTS trace_events_no_delete
            BEFORE DELETE ON trace_events BEGIN
                SELECT RAISE(ABORT, 'Рӯйдоди аудитро нест кардан мумкин нест');
            END;
            CREATE TRIGGER IF NOT EXISTS product_revisions_no_update
            BEFORE UPDATE ON product_revisions BEGIN
                SELECT RAISE(ABORT, 'Версияи маҳсулотро тағйир додан мумкин нест');
            END;
            CREATE TRIGGER IF NOT EXISTS product_revisions_no_delete
            BEFORE DELETE ON product_revisions BEGIN
                SELECT RAISE(ABORT, 'Версияи маҳсулотро нест кардан мумкин нест');
            END;
            """
        )
        catalog_columns = {row["name"] for row in db.execute("PRAGMA table_info(product_catalog)")}
        if "specifications" not in catalog_columns:
            db.execute(
                "ALTER TABLE product_catalog ADD COLUMN specifications TEXT NOT NULL DEFAULT '[]'"
            )
        seed_product_catalog(db)
        if not db.execute("SELECT 1 FROM schema_migrations WHERE version=1").fetchone():
            if not db.execute("SELECT COUNT(*) FROM locations").fetchone()[0]:
                seed_database(db)
            db.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(1,?)", (now(),))
        if not db.execute("SELECT 1 FROM schema_migrations WHERE version=2").fetchone():
            columns = {row["name"] for row in db.execute("PRAGMA table_info(products)")}
            if "catalog_id" not in columns:
                db.execute(
                    "ALTER TABLE products ADD COLUMN catalog_id INTEGER REFERENCES product_catalog(id)"
                )
            marker = db.execute(
                "SELECT id FROM product_catalog WHERE sku='TJ-MRK-001'"
            ).fetchone()
            db.execute(
                "UPDATE products SET catalog_id=? WHERE catalog_id IS NULL",
                (marker["id"],),
            )
            db.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
                (2, now()),
            )
        if not db.execute("SELECT 1 FROM schema_migrations WHERE version=?", (SCHEMA_VERSION,)).fetchone():
            for sku, _, _, _, model, _ in PRODUCT_CATALOG_SEED:
                db.execute(
                    "UPDATE product_catalog SET model=?,specifications=? WHERE sku=?",
                    (model, canonical_json(catalog_specifications(model)), sku),
                )
            legacy_markers = {"length": 12, "diameter": 1.8, "shape": "faceted"}
            marker = db.execute(
                "SELECT id FROM product_catalog WHERE sku='TJ-MRK-001'"
            ).fetchone()
            for row in db.execute(
                "SELECT id,config FROM products WHERE config LIKE '%\"type\":\"marker\"%'"
            ):
                configuration = json.loads(row["config"])
                db.execute(
                    "UPDATE products SET catalog_id=? WHERE id=? AND catalog_id IS NULL",
                    (marker["id"], row["id"]),
                )
                changed = False
                for key, value in legacy_markers.items():
                    if key not in configuration:
                        configuration[key] = value
                        changed = True
                if changed:
                    db.execute(
                        "UPDATE products SET config=? WHERE id=?",
                        (canonical_json(configuration), row["id"]),
                    )
            db.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
                (SCHEMA_VERSION, now()),
            )


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(title="Supply Tail Tajik", version="1.0.0", lifespan=lifespan)


class LocationInput(BaseModel):
    name: str = Field(min_length=2)
    kind: str
    region: str
    address: str
    city: str


class StaffInput(BaseModel):
    name: str = Field(min_length=2)
    role: str
    employee_no: str
    phone: str


class VehicleInput(BaseModel):
    plate: str
    brand: str
    driver: str
    driver_phone: str
    capacity: float = Field(gt=0)
    refrigerated: bool = False


class RouteInput(BaseModel):
    name: str
    origin: str
    destination: str
    distance_km: float = Field(gt=0)
    duration_hours: float = Field(gt=0)


class CatalogAttributeInput(BaseModel):
    key: str = Field(pattern=r"^attribute_[a-z0-9_]{1,39}$")
    label: str = Field(min_length=1, max_length=80)
    type: Literal["number", "select", "text"]
    default: Any
    unit: str = Field(default="", max_length=24)
    min_value: float | None = None
    max_value: float | None = None
    step: float = Field(default=1, gt=0)
    options: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def validate_attribute(self) -> "CatalogAttributeInput":
        if self.type == "number":
            if self.min_value is None or self.max_value is None or self.max_value <= self.min_value:
                raise ValueError("Numeric attributes need a valid minimum and maximum")
            if isinstance(self.default, bool) or not isinstance(self.default, (int, float)):
                raise ValueError("Numeric attributes need a numeric default")
            if not all(math.isfinite(value) for value in (self.min_value, self.max_value, self.step, self.default)):
                raise ValueError("Numeric attributes must use finite values")
            if not self.min_value <= self.default <= self.max_value:
                raise ValueError("Numeric defaults must be within the configured range")
        elif self.type == "select":
            self.options = [option.strip() for option in self.options if option.strip()]
            if len(self.options) < 2 or len(set(self.options)) != len(self.options):
                raise ValueError("Select attributes need at least two unique options")
            if not isinstance(self.default, str) or self.default not in self.options:
                raise ValueError("The default must match one of the select options")
        elif not isinstance(self.default, str) or len(self.default) > 500:
            raise ValueError("Text attributes need a string default of at most 500 characters")
        return self


class CatalogProductInput(BaseModel):
    sku: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=2, max_length=120)
    category: str = Field(min_length=2, max_length=80)
    unit: str = Field(min_length=1, max_length=24)
    model: Literal[
        "marker", "pen", "pencil", "notebook", "folder", "bottle",
        "box", "ruler", "eraser", "mug", "soap",
    ] = "box"
    description: str = Field(default="", max_length=500)
    attributes: list[CatalogAttributeInput] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_attribute_keys(self) -> "CatalogProductInput":
        keys = [attribute.key for attribute in self.attributes]
        reserved = {specification["key"] for specification in MODEL_SPECS[self.model]}
        if len(set(keys)) != len(keys) or reserved.intersection(keys):
            raise ValueError("Attribute keys must be unique and not conflict with built-in attributes")
        return self


class ProductInput(BaseModel):
    barcode: str = Field(min_length=8, max_length=32)
    batch_id: str = Field(min_length=2)
    catalog_id: int
    config: dict[str, Any]
    weight: float = Field(gt=0)
    qc_status: str
    note: str = ""
    factory_id: int
    responsible_id: int
    operator_id: int


class EventInput(BaseModel):
    stage: str
    location_id: int
    responsible_id: int
    operator_id: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    config_revision: dict[str, Any] | None = None
    revision_note: str = ""


RESOURCE_COLUMNS = {
    "locations": "id,name,kind,region,address,city",
    "staff": "id,name,role,employee_no,phone",
    "vehicles": "id,plate,brand,driver,driver_phone,capacity,refrigerated",
    "routes": "id,name,origin,destination,distance_km,duration_hours",
    "product_catalog": "id,sku,name,category,unit,model,description,specifications",
}
RESOURCE_INPUTS = {
    "locations": LocationInput,
    "staff": StaffInput,
    "vehicles": VehicleInput,
    "routes": RouteInput,
    "product_catalog": CatalogProductInput,
}


def parse_product(row: sqlite3.Row) -> dict[str, Any]:
    product = dict(row)
    product["config"] = json.loads(product["config"])
    return product


def parse_resource(resource: str, row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    if resource == "product_catalog":
        result["specifications"] = json.loads(result["specifications"])
    return result


def validate_product_configuration(
    configuration: dict[str, Any],
    catalog_product: sqlite3.Row,
) -> None:
    model = catalog_product["model"]
    if configuration.get("type") != model:
        raise HTTPException(422, "Танзимоти намуна ба навъи маҳсулоти интихобшуда мувофиқат намекунад")
    specifications = json.loads(catalog_product["specifications"])
    allowed_keys = {"type", "color", "capColor"} | {
        specification["key"] for specification in specifications
    }
    if set(configuration) - allowed_keys:
        raise HTTPException(422, "Танзимот дорои параметрҳои ношинос аст")
    for key in ("color", "capColor"):
        if not isinstance(configuration.get(key), str) or not re.fullmatch(
            r"#[0-9a-fA-F]{6}", configuration[key]
        ):
            raise HTTPException(422, f"Ранги «{key}» нодуруст аст")
    for specification in specifications:
        key = specification["key"]
        if key not in configuration:
            raise HTTPException(422, f"Параметри ҳатмии «{specification['label']}» пур нашудааст")
        value = configuration[key]
        if specification["type"] in ("range", "number"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise HTTPException(422, f"Қимати «{specification['label']}» бояд адад бошад")
            if isinstance(value, float) and not math.isfinite(value):
                raise HTTPException(422, f"Қимати «{specification['label']}» бояд адади ниҳоӣ бошад")
            if not specification["min"] <= value <= specification["max"]:
                raise HTTPException(422, f"Қимати «{specification['label']}» аз ҳудуди иҷозатдодашуда берун аст")
        elif specification["type"] == "select":
            allowed = {option["value"] for option in specification["options"]}
            if not isinstance(value, str) or value not in allowed:
                raise HTTPException(422, f"Қимати «{specification['label']}» интихоб нашудааст")
        elif specification["type"] == "text":
            if not isinstance(value, str) or len(value) > 500:
                raise HTTPException(422, f"Матни «{specification['label']}» нодуруст аст")


def validate_resource(resource: str) -> None:
    if resource not in RESOURCE_COLUMNS:
        raise HTTPException(404, "Маълумотнома ёфт нашуд")


@app.get("/api/bootstrap")
def bootstrap() -> dict[str, Any]:
    with connect() as db:
        result = {key: [parse_resource(key, row) for row in db.execute(f"SELECT {columns} FROM {key} ORDER BY id")] for key, columns in RESOURCE_COLUMNS.items()}
        result["products"] = [parse_product(row) for row in db.execute("SELECT * FROM products ORDER BY id DESC")]
        result["schema_version"] = SCHEMA_VERSION
        result["schema_history"] = [dict(row) for row in db.execute("SELECT * FROM schema_migrations ORDER BY version")]
        return result


@app.get("/api/mdm/{resource}")
def list_resource(resource: str) -> list[dict[str, Any]]:
    validate_resource(resource)
    with connect() as db:
        return [parse_resource(resource, row) for row in db.execute(f"SELECT {RESOURCE_COLUMNS[resource]} FROM {resource} ORDER BY id")]


@app.post("/api/mdm/{resource}", status_code=201)
def create_resource(resource: str, payload: dict[str, Any]) -> dict[str, Any]:
    validate_resource(resource)
    try:
        validated = RESOURCE_INPUTS[resource].model_validate(payload)
        values = validated.model_dump(exclude={"attributes"} if resource == "product_catalog" else None)
    except ValidationError as error:
        raise HTTPException(422, "Маълумоти воридшуда нодуруст аст") from error
    if resource == "vehicles":
        values["refrigerated"] = int(values["refrigerated"])
    if resource == "product_catalog":
        attributes = validated.attributes
        specifications = catalog_specifications(values["model"])
        specifications.extend(attribute_specification(attribute) for attribute in attributes)
        values["specifications"] = canonical_json(specifications)
    columns = list(values)
    with connect() as db:
        try:
            cursor = db.execute(
                f"INSERT INTO {resource}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                [values[column] for column in columns],
            )
        except sqlite3.IntegrityError as error:
            raise HTTPException(409, "Сабт бо ин рамз аллакай вуҷуд дорад") from error
        row = db.execute(f"SELECT {RESOURCE_COLUMNS[resource]} FROM {resource} WHERE id=?", (cursor.lastrowid,)).fetchone()
        return parse_resource(resource, row)


@app.post("/api/mdm/product_catalog/{catalog_id}/attributes", status_code=201)
def add_catalog_attribute(catalog_id: int, payload: CatalogAttributeInput) -> dict[str, Any]:
    with connect() as db:
        product = db.execute(
            "SELECT id,model,specifications FROM product_catalog WHERE id=?",
            (catalog_id,),
        ).fetchone()
        if not product:
            raise HTTPException(404, "Маҳсулот дар феҳристи молҳо ёфт нашуд")
        specifications = json.loads(product["specifications"])
        if any(specification["key"] == payload.key for specification in specifications):
            raise HTTPException(409, "Атрибут бо ҳамин рамз аллакай вуҷуд дорад")
        if len(specifications) - len(MODEL_SPECS[product["model"]]) >= 20:
            raise HTTPException(409, "Ба як маҳсулот то 20 атрибути иловагӣ илова кардан мумкин аст")
        specifications.append(attribute_specification(payload))
        db.execute(
            "UPDATE product_catalog SET specifications=? WHERE id=?",
            (canonical_json(specifications), catalog_id),
        )
        return {
            **dict(db.execute(
                f"SELECT {RESOURCE_COLUMNS['product_catalog']} FROM product_catalog WHERE id=?",
                (catalog_id,),
            ).fetchone()),
            "specifications": specifications,
        }


@app.get("/api/products")
def list_products() -> list[dict[str, Any]]:
    with connect() as db:
        return [parse_product(row) for row in db.execute("SELECT * FROM products ORDER BY id DESC")]


@app.post("/api/products", status_code=201)
def create_product(payload: ProductInput) -> dict[str, Any]:
    with connect() as db:
        if db.execute("SELECT 1 FROM products WHERE barcode=?", (payload.barcode,)).fetchone():
            raise HTTPException(409, "Маҳсулот бо ин рақами штрихкод аллакай сабт шудааст")
        catalog_product = db.execute(
            "SELECT id,name,model,specifications FROM product_catalog WHERE id=?",
            (payload.catalog_id,),
        ).fetchone()
        if not catalog_product:
            raise HTTPException(404, "Маҳсулот дар феҳристи молҳо ёфт нашуд")
        validate_product_configuration(payload.config, catalog_product)
        cursor = db.execute(
            "INSERT INTO products(barcode,batch_id,name,config,config_version,weight,qc_status,note,current_stage,revision,created_at,schema_version,catalog_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (payload.barcode, payload.batch_id, catalog_product["name"], canonical_json(payload.config), 1, payload.weight, payload.qc_status, payload.note, "factory", 1, now(), SCHEMA_VERSION, payload.catalog_id),
        )
        product_id = cursor.lastrowid
        add_revision(db, product_id, payload.config, payload.operator_id, "Танзимоти ибтидоии корхона")
        insert_event(db, product_id, "factory", payload.factory_id, payload.responsible_id, payload.operator_id, {"weight_kg": payload.weight, "quality": payload.qc_status, "note": payload.note})
        return parse_product(db.execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone())


@app.get("/api/products/{barcode}")
def get_product(barcode: str) -> dict[str, Any]:
    with connect() as db:
        row = db.execute("SELECT * FROM products WHERE barcode=?", (barcode,)).fetchone()
        if not row:
            raise HTTPException(404, "Маҳсулот ёфт нашуд")
        product = parse_product(row)
        events = db.execute(
            "SELECT e.*,l.name AS location_name,l.region,l.city,s.name AS responsible_name,s.role AS responsible_role,o.name AS operator_name FROM trace_events e JOIN locations l ON l.id=e.location_id JOIN staff s ON s.id=e.responsible_id JOIN staff o ON o.id=e.operator_id WHERE e.product_id=? ORDER BY e.sequence",
            (row["id"],),
        )
        product["events"] = [{**dict(event), "metadata": json.loads(event["metadata"])} for event in events]
        revisions = db.execute(
            "SELECT r.version,r.config,r.note,r.created_at,s.name AS changed_by FROM product_revisions r JOIN staff s ON s.id=r.changed_by WHERE r.product_id=? ORDER BY r.version",
            (row["id"],),
        )
        product["revisions"] = [{**dict(revision), "config": json.loads(revision["config"])} for revision in revisions]
        return product


@app.post("/api/products/{barcode}/events", status_code=201)
def add_event(barcode: str, payload: EventInput) -> dict[str, Any]:
    if payload.stage not in STAGE_ORDER or payload.stage == "factory":
        raise HTTPException(422, "Марҳилаи интихобшуда нодуруст аст")
    with connect() as db:
        product = db.execute("SELECT * FROM products WHERE barcode=?", (barcode,)).fetchone()
        if not product:
            raise HTTPException(404, "Маҳсулот ёфт нашуд")
        expected = STAGE_ORDER[product["current_stage"]] + 1
        if STAGE_ORDER[payload.stage] != expected:
            raise HTTPException(409, f"Гузариш ба марҳилаи «{STAGE_TITLES[payload.stage]}» иҷозат нест. Марҳилаи ҷорӣ: «{STAGE_TITLES[product['current_stage']]}».")
        config_version = product["config_version"]
        if payload.config_revision:
            if payload.stage != "workshop":
                raise HTTPException(422, "Танҳо дар сех версияи нави танзимот сабт мешавад")
            config_version = add_revision(db, product["id"], payload.config_revision, payload.responsible_id, payload.revision_note or "Тағйироти марҳилаи сех")
            db.execute("UPDATE products SET config_version=?,revision=revision+1 WHERE id=?", (config_version, product["id"]))
        event = insert_event(db, product["id"], payload.stage, payload.location_id, payload.responsible_id, payload.operator_id, {**payload.metadata, "config_version": config_version})
        db.execute("UPDATE products SET current_stage=?,revision=revision+1 WHERE id=?", (payload.stage, product["id"]))
        return dict(event)


@app.get("/api/health")
def health() -> dict[str, Any]:
    with connect() as db:
        products = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        events = db.execute("SELECT COUNT(*) FROM trace_events").fetchone()[0]
    return {"status": "ok", "database": DB_PATH.name, "products": products, "events": events, "schema_version": SCHEMA_VERSION}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")