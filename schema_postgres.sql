-- =============================================================================
-- Treking-V2: Корпоративная Схема Базы Данных PostgreSQL
-- Разработано для высоконагруженных систем (>500k операций в месяц)
-- Поддержка WORM (Write Once, Read Many), SHA-256 аудита и RBAC
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. Справочник подразделений / локаций (цеха, склады, магазины)
CREATE TABLE IF NOT EXISTS locations (
    id SERIAL PRIMARY KEY,
    name VARCHAR(150) NOT NULL,
    kind VARCHAR(50) NOT NULL CHECK (kind IN ('workshop', 'warehouse', 'store', 'transit')),
    city VARCHAR(100) NOT NULL,
    address VARCHAR(255) NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);

-- 2. Сотрудники и операторы
CREATE TABLE IF NOT EXISTS staff (
    id SERIAL PRIMARY KEY,
    name VARCHAR(150) NOT NULL,
    role VARCHAR(100) NOT NULL,
    employee_no VARCHAR(50) NOT NULL,
    phone VARCHAR(50) NOT NULL
);

-- 3. Транспортные средства
CREATE TABLE IF NOT EXISTS vehicles (
    id SERIAL PRIMARY KEY,
    plate VARCHAR(50) NOT NULL,
    brand VARCHAR(100) NOT NULL,
    driver VARCHAR(150) NOT NULL,
    driver_phone VARCHAR(50) NOT NULL,
    capacity NUMERIC(10,2) NOT NULL,
    refrigerated INTEGER NOT NULL DEFAULT 0
);

-- 4. Логистические маршруты
CREATE TABLE IF NOT EXISTS routes (
    id SERIAL PRIMARY KEY,
    name VARCHAR(150) NOT NULL,
    origin VARCHAR(100) NOT NULL,
    destination VARCHAR(100) NOT NULL,
    distance_km NUMERIC(10,2) NOT NULL,
    duration_hours NUMERIC(10,2) NOT NULL
);

-- 5. Пользовательские атрибуты и флаги изделий
CREATE TABLE IF NOT EXISTS custom_attributes (
    id SERIAL PRIMARY KEY,
    key VARCHAR(50) UNIQUE NOT NULL,
    name VARCHAR(100) NOT NULL,
    type VARCHAR(30) NOT NULL CHECK (type IN ('text', 'select', 'number', 'date', 'boolean')),
    options TEXT NOT NULL DEFAULT '[]',
    unit VARCHAR(20) NOT NULL DEFAULT '',
    is_system INTEGER NOT NULL DEFAULT 0
);

-- 6. Номенклатура / Товары
CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    barcode VARCHAR(50) UNIQUE NOT NULL,
    sku VARCHAR(50) UNIQUE NOT NULL,
    name VARCHAR(200) NOT NULL,
    category VARCHAR(100) NOT NULL DEFAULT '',
    unit VARCHAR(20) NOT NULL DEFAULT 'шт',
    price NUMERIC(12,2) NOT NULL DEFAULT 0.0,
    cost_price NUMERIC(12,2) NOT NULL DEFAULT 0.0,
    weight NUMERIC(10,3) NOT NULL DEFAULT 0.0,
    qc_status VARCHAR(50) NOT NULL DEFAULT 'Пройден',
    current_stage VARCHAR(50) NOT NULL DEFAULT 'production',
    batch_id VARCHAR(50) NOT NULL DEFAULT '',
    production_date VARCHAR(50) NOT NULL DEFAULT '',
    expiry_date VARCHAR(50) NOT NULL DEFAULT '',
    is_certified INTEGER NOT NULL DEFAULT 1,
    is_fragile INTEGER NOT NULL DEFAULT 0,
    is_temperature_controlled INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    description TEXT NOT NULL DEFAULT '',
    attributes TEXT NOT NULL DEFAULT '{}',
    created_at VARCHAR(50) NOT NULL,
    updated_at VARCHAR(50) NOT NULL
);

-- 7. Остатки продукции по складам и цехам
CREATE TABLE IF NOT EXISTS workshop_stock (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    quantity INTEGER NOT NULL DEFAULT 0,
    updated_at VARCHAR(50) NOT NULL,
    UNIQUE(product_id, location_id)
);

-- 8. События трекинга изделий (WORM блокчейн-цепочка)
CREATE TABLE IF NOT EXISTS trace_events (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    stage VARCHAR(50) NOT NULL,
    action VARCHAR(100) NOT NULL,
    from_location_id INTEGER REFERENCES locations(id),
    to_location_id INTEGER REFERENCES locations(id),
    responsible_id INTEGER REFERENCES staff(id),
    operator_id INTEGER REFERENCES staff(id),
    quantity INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    timestamp VARCHAR(50) NOT NULL,
    previous_hash VARCHAR(64) NOT NULL,
    event_hash VARCHAR(64) NOT NULL,
    UNIQUE(product_id, sequence)
);

-- 9. Категории
CREATE TABLE IF NOT EXISTS categories (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL
);

-- 10. Единицы измерения
CREATE TABLE IF NOT EXISTS units (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) UNIQUE NOT NULL,
    description VARCHAR(100) NOT NULL DEFAULT ''
);

-- 11. Корпоративные пользователи и RBAC
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(100) NOT NULL,
    role VARCHAR(20) NOT NULL CHECK (role IN ('admin', 'manager', 'storekeeper', 'qc', 'worker')),
    workshop_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at VARCHAR(50) NOT NULL
);

-- 12. Неизменяемый журнал аудита операций (Audit Trail / SHA-256 Ledger)
CREATE TABLE IF NOT EXISTS audit_logs (
    id SERIAL PRIMARY KEY,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action_type VARCHAR(50) NOT NULL,
    target_entity VARCHAR(50) NOT NULL,
    payload TEXT NOT NULL,
    prev_hash VARCHAR(64) NOT NULL,
    current_hash VARCHAR(64) NOT NULL,
    created_at VARCHAR(50) NOT NULL
);

-- =============================================================================
-- Индексы для обеспечения высокой производительности (>500k операций/месяц)
-- =============================================================================
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_users_active ON users(is_active);

CREATE INDEX IF NOT EXISTS idx_audit_logs_user_date ON audit_logs(created_at DESC, user_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_target ON audit_logs(target_entity, action_type);
CREATE INDEX IF NOT EXISTS idx_audit_logs_chain ON audit_logs(prev_hash, current_hash);

CREATE INDEX IF NOT EXISTS idx_products_barcode ON products(barcode);
CREATE INDEX IF NOT EXISTS idx_products_sku ON products(sku);
CREATE INDEX IF NOT EXISTS idx_products_category ON products(category);
CREATE INDEX IF NOT EXISTS idx_products_stage ON products(current_stage);

CREATE INDEX IF NOT EXISTS idx_workshop_stock_loc_prod ON workshop_stock(location_id, product_id);
CREATE INDEX IF NOT EXISTS idx_trace_events_prod_seq ON trace_events(product_id, sequence);
CREATE INDEX IF NOT EXISTS idx_trace_events_stage ON trace_events(stage);

-- =============================================================================
-- Триггеры неизменяемости (WORM - Write Once, Read Many)
-- =============================================================================

CREATE OR REPLACE FUNCTION enforce_worm_audit() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Записи аудита неизменяемы (WORM audit trail). Операция % запрещена!', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_logs_immutable ON audit_logs;
CREATE TRIGGER trg_audit_logs_immutable
BEFORE UPDATE OR DELETE ON audit_logs
FOR EACH ROW EXECUTE FUNCTION enforce_worm_audit();

DROP TRIGGER IF EXISTS trg_trace_events_immutable ON trace_events;
CREATE TRIGGER trg_trace_events_immutable
BEFORE UPDATE OR DELETE ON trace_events
FOR EACH ROW EXECUTE FUNCTION enforce_worm_audit();
