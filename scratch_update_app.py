with open("app.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Add UnitInput Pydantic model
unit_input_model = """class UnitInput(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    description: str = Field(default="", max_length=100)


class CategoryInput(BaseModel):"""

content = content.replace("class CategoryInput(BaseModel):", unit_input_model)

# 2. In initialize_database(), add units table and default switch attributes
units_db_init = """        db.execute(\"\"\"
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            );
        \"\"\")

        db.execute(\"\"\"
            CREATE TABLE IF NOT EXISTS units (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                description TEXT NOT NULL DEFAULT ''
            );
        \"\"\")

        # Seed initial units if empty
        if db.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 0:
            default_units = [
                ("шт", "Штука"),
                ("компл", "Комплект"),
                ("пар", "Пара"),
                ("упак", "Упаковка"),
                ("м", "Метр"),
                ("пог. м", "Погонный метр"),
                ("кг", "Килограмм"),
                ("рулон", "Рулон"),
                ("бобина", "Бобина"),
                ("л", "Литр"),
                ("т", "Тонна"),
            ]
            db.executemany("INSERT OR IGNORE INTO units(name, description) VALUES(?,?)", default_units)

        # Ensure default switch flags exist in custom_attributes with is_system=0 (fully editable/deletable)
        default_switches = [
            ("is_certified", "Сертификат качества", "boolean"),
            ("is_active", "Активен для продажи", "boolean"),
            ("is_fragile", "Хрупкий груз", "boolean"),
            ("is_temperature_controlled", "Температурный режим", "boolean"),
        ]
        for key, name, attr_type in default_switches:
            if not db.execute("SELECT 1 FROM custom_attributes WHERE key=?", (key,)).fetchone():
                db.execute(
                    "INSERT INTO custom_attributes(key, name, type, options, unit, is_system) VALUES(?,?,?,'[]','',0)",
                    (key, name, attr_type),
                )"""

content = content.replace("""        db.execute(\"\"\"
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            );
        \"\"\")""", units_db_init)

# 3. In bootstrap(), include units
old_bootstrap_return = """        categories_db = [r["name"] for r in db.execute("SELECT name FROM categories ORDER BY name").fetchall()]
        categories_products = [r[0] for r in db.execute("SELECT DISTINCT category FROM products WHERE category != ''").fetchall()]
        all_categories = sorted(list(set(categories_db + categories_products)))

        return {
            "country": "Таджикистан",
            "currency": "сомони",
            "products": products,
            "locations": locations,
            "staff": staff,
            "vehicles": vehicles,
            "routes": routes,
            "custom_attributes": attributes,
            "categories": all_categories,"""

new_bootstrap_return = """        categories_db = [r["name"] for r in db.execute("SELECT name FROM categories ORDER BY name").fetchall()]
        categories_products = [r[0] for r in db.execute("SELECT DISTINCT category FROM products WHERE category != ''").fetchall()]
        all_categories = sorted(list(set(categories_db + categories_products)))

        units_rows = db.execute("SELECT * FROM units ORDER BY name").fetchall()
        units_db = [dict(r) for r in units_rows]
        prod_units = {r[0] for r in db.execute("SELECT DISTINCT unit FROM products WHERE unit != ''").fetchall()}
        existing_unit_names = {u["name"] for u in units_db}
        for pu in sorted(prod_units):
            if pu not in existing_unit_names:
                units_db.append({"id": None, "name": pu, "description": ""})

        return {
            "country": "Таджикистан",
            "currency": "сомони",
            "products": products,
            "locations": locations,
            "staff": staff,
            "vehicles": vehicles,
            "routes": routes,
            "custom_attributes": attributes,
            "categories": all_categories,
            "units": units_db,"""

content = content.replace(old_bootstrap_return, new_bootstrap_return)

# 4. In create_product, auto-register any new custom unit
old_create_cat_block = """        cat = payload.category.strip()
        if cat:
            db.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", (cat,))"""

new_create_cat_block = """        cat = payload.category.strip()
        if cat:
            db.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", (cat,))

        u_name = payload.unit.strip()
        if u_name:
            db.execute("INSERT OR IGNORE INTO units(name, description) VALUES(?, '')", (u_name,))"""

content = content.replace(old_create_cat_block, new_create_cat_block)

# 5. Add Units API endpoints before health
units_api_endpoints = """# --- Units of Measurement Management ---

@app.get("/api/units")
def list_units() -> list[dict[str, Any]]:
    with connect() as db:
        rows = db.execute("SELECT * FROM units ORDER BY name").fetchall()
        counts = dict(db.execute("SELECT unit, COUNT(*) FROM products WHERE unit != '' GROUP BY unit").fetchall())
        db_units = [dict(r) for r in rows]
        for u in db_units:
            u["product_count"] = counts.get(u["name"], 0)
        unit_names = {u["name"] for u in db_units}
        for uname, cnt in counts.items():
            if uname not in unit_names:
                db_units.append({"id": None, "name": uname, "description": "", "product_count": cnt})
        return sorted(db_units, key=lambda x: x["name"])


@app.post("/api/units", status_code=201)
def create_unit(payload: UnitInput) -> dict[str, Any]:
    name = payload.name.strip()
    with connect() as db:
        if db.execute("SELECT 1 FROM units WHERE name=?", (name,)).fetchone():
            raise HTTPException(409, f"Единица измерения «{name}» уже существует")
        cur = db.execute("INSERT INTO units(name, description) VALUES(?,?)", (name, payload.description.strip()))
        return {"id": cur.lastrowid, "name": name, "description": payload.description.strip(), "product_count": 0}


@app.put("/api/units/{unit_id}")
def update_unit(unit_id: int, payload: UnitInput) -> dict[str, Any]:
    new_name = payload.name.strip()
    with connect() as db:
        old = db.execute("SELECT name FROM units WHERE id=?", (unit_id,)).fetchone()
        if not old:
            raise HTTPException(404, "Единица измерения не найдена")
        old_name = old["name"]
        db.execute("UPDATE units SET name=?, description=? WHERE id=?", (new_name, payload.description.strip(), unit_id))
        db.execute("UPDATE products SET unit=? WHERE unit=?", (new_name, old_name))
        return {"id": unit_id, "name": new_name, "description": payload.description.strip()}


@app.delete("/api/units/{unit_id}")
def delete_unit(unit_id: int) -> dict[str, str]:
    with connect() as db:
        old = db.execute("SELECT name FROM units WHERE id=?", (unit_id,)).fetchone()
        if not old:
            raise HTTPException(404, "Единица измерения не найдена")
        unit_name = old["name"]
        db.execute("DELETE FROM units WHERE id=?", (unit_id,))
        return {"status": "ok", "message": f"Единица измерения «{unit_name}» удалена"}


"""

content = content.replace("@app.get(\"/api/health\")", units_api_endpoints + "@app.get(\"/api/health\")")

with open("app.py", "w", encoding="utf-8") as f:
    f.write(content)

print("app.py updated with units and switches support!")
