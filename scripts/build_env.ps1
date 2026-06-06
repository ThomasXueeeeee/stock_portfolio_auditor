param(
    [string]$Name = "perf_audit"
)

conda env create -n $Name -f environment.lock.yml
