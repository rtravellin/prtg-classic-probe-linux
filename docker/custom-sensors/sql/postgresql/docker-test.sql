-- PRTG PostgreSQL v2 sensor test query (Docker probe under Wine-Mono).
-- The v2 SQL sensors read their query from a .sql file in this folder
-- (Custom Sensors\sql\postgresql\) rather than inline. This returns a single
-- scalar so the sensor has one numeric channel proving the SQLv2.exe helper +
-- bundled Npgsql driver work end-to-end on the Wine probe. See DOTNET-ENGINE-C.md.
SELECT 42 AS answer;
