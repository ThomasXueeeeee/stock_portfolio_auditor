param(
    [switch]$All
)

if ($All) {
    pytest
} else {
    pytest -m "not local_data"
}
