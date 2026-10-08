CREATE TABLE brands (brand_id INTEGER PRIMARY KEY, brand_name TEXT NOT NULL);
CREATE TABLE categories (category_id INTEGER PRIMARY KEY, category_name TEXT NOT NULL);
CREATE TABLE stores (
 store_id INTEGER PRIMARY KEY, store_name TEXT NOT NULL, phone TEXT, email TEXT,
 street TEXT, city TEXT, state TEXT, zip_code TEXT
);
CREATE TABLE customers (
 customer_id INTEGER PRIMARY KEY, first_name TEXT NOT NULL, last_name TEXT NOT NULL,
 phone TEXT, email TEXT, street TEXT, city TEXT, state TEXT, zip_code TEXT
);
CREATE TABLE staffs (
 staff_id INTEGER PRIMARY KEY, first_name TEXT NOT NULL, last_name TEXT NOT NULL,
 email TEXT, phone TEXT, active INTEGER CHECK(active IN (0,1)),
 store_id INTEGER NOT NULL REFERENCES stores(store_id),
 manager_id INTEGER REFERENCES staffs(staff_id)
);
CREATE TABLE products (
 product_id INTEGER PRIMARY KEY, product_name TEXT NOT NULL,
 brand_id INTEGER NOT NULL REFERENCES brands(brand_id),
 category_id INTEGER NOT NULL REFERENCES categories(category_id),
 model_year INTEGER, list_price REAL NOT NULL CHECK(list_price >= 0)
);
CREATE TABLE orders (
 order_id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
 order_status INTEGER NOT NULL CHECK(order_status IN (1,2,3,4)),
 order_date TEXT NOT NULL, required_date TEXT, shipped_date TEXT,
 store_id INTEGER NOT NULL REFERENCES stores(store_id),
 staff_id INTEGER NOT NULL REFERENCES staffs(staff_id)
);
CREATE TABLE order_items (
 order_id INTEGER NOT NULL REFERENCES orders(order_id), item_id INTEGER NOT NULL,
 product_id INTEGER NOT NULL REFERENCES products(product_id),
 quantity INTEGER NOT NULL CHECK(quantity > 0), list_price REAL NOT NULL CHECK(list_price >= 0),
 discount REAL NOT NULL CHECK(discount BETWEEN 0 AND 1), PRIMARY KEY(order_id,item_id)
);
CREATE TABLE stocks (
 store_id INTEGER NOT NULL REFERENCES stores(store_id),
 product_id INTEGER NOT NULL REFERENCES products(product_id),
 quantity INTEGER NOT NULL CHECK(quantity >= 0), PRIMARY KEY(store_id,product_id)
);
CREATE INDEX orders_customer ON orders(customer_id);
CREATE INDEX orders_store ON orders(store_id);
CREATE INDEX orders_date ON orders(order_date);
CREATE INDEX items_product ON order_items(product_id);
