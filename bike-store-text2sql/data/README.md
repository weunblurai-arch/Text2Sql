# Dataset provenance

The nine files in `csv/` were downloaded unchanged on 2026-10-08 from the public dataset cited in the problem statement:

[Bike Store Relational Database | SQL — Dillon Myrick, Kaggle](https://www.kaggle.com/datasets/dillonmyrick/bike-store-sample-database/data).

`schema.sql` loads them into SQLite with types, primary keys, foreign keys, and constraints. Generated databases are excluded from source control and the ZIP. Attribution and redistribution conditions remain those specified by the source dataset.

UC10 validation and ground-truth files were not included in the supplied materials. The cases in `validation/examples.json` are only the three examples from the problem statement.
