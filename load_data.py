import subprocess
import os
import tarfile
import urllib.request
import zipfile

DATASETS = {
    "openstack": {
        "url": "https://zenodo.org/records/8196385/files/OpenStack.tar.gz?download=1",
        "archive": "OpenStack.tar.gz",
        "extract_dir": ".",
        # File that signals the dataset is already present (skip download).
        "marker": "openstack_abnormal.log",
    },
    "bgl": {
        "url": "https://zenodo.org/records/8196385/files/BGL.zip?download=1",
        "archive": "BGL_dataset/BGL.zip",
        "extract_dir": "BGL_dataset",
        "marker": "BGL_dataset/BGL.log",
    },
}

def run_cmd(cmd):
    print(f"Running: {cmd[:60]}...")
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Error: {res.stderr}")
    else:
        print(res.stdout.strip())

def _extract(archive_path, extract_dir):
    # Extract a .zip or .tar.gz archive into extract_dir
    os.makedirs(extract_dir, exist_ok=True)
    if archive_path.endswith(".zip"):
        with zipfile.ZipFile(archive_path) as z:
            z.extractall(extract_dir)
    elif archive_path.endswith((".tar.gz", ".tgz")):
        with tarfile.open(archive_path, "r:gz") as t:
            t.extractall(extract_dir)
    else:
        raise ValueError(f"Unsupported archive format: {archive_path}")

def ensure_dataset(name):
    # Ensure a loghub dataset is present locally
    # downloading and extract from Zenodo on demand
    # Returns True if the dataset marker file exists.
    cfg = DATASETS[name]
    marker = cfg["marker"]
    if os.path.exists(marker):
        print(f"[{name}] already present at {marker}")
        return True

    archive_path = cfg["archive"]
    env_override = os.environ.get(f"{name.upper()}_ARCHIVE")
    if env_override and os.path.exists(env_override):
        print(f"[{name}] using local archive from env: {env_override}")
        archive_path = env_override
    elif os.path.exists(archive_path):
        print(f"[{name}] using existing archive: {archive_path}")
    else:
        os.makedirs(os.path.dirname(archive_path) or ".", exist_ok=True)
        print(f"[{name}] downloading from {cfg['url']} ...")
        try:
            urllib.request.urlretrieve(cfg["url"], archive_path)
        except Exception as e:
            print(f"[{name}] ERROR: download failed: {e}")
            return False

    print(f"[{name}] extracting {archive_path} -> {cfg['extract_dir']}")
    try:
        _extract(archive_path, cfg["extract_dir"])
    except Exception as e:
        print(f"[{name}] ERROR: extraction failed: {e}")
        return False

    if not os.path.exists(marker):
        print(f"[{name}] ERROR: expected {marker} after extraction, not found")
        return False
    print(f"[{name}] ready: {marker}")
    return True

def main():
    print("=== Initializing SereneDB Schema & Ingestion ===")
    
    # 1. Create Tables
    schema_sql = """
    CREATE TABLE IF NOT EXISTS openstack (
        log_date TEXT, log_time TEXT, pid TEXT, level TEXT, component TEXT, content TEXT
    );
    CREATE TABLE IF NOT EXISTS bgl (
        alert_flag TEXT, timestamp BIGINT, date_str TEXT, node TEXT, time_str TEXT, 
        node_repeat TEXT, log_type TEXT, component TEXT, level TEXT, content TEXT
    );
    CREATE TABLE IF NOT EXISTS team_ownership (
        component TEXT PRIMARY KEY, team_name TEXT, lead_contact TEXT
    );
    INSERT INTO team_ownership VALUES
        ('nova-api', 'Compute Platform Team', 'compute-lead@platform.internal'),
        ('nova-compute', 'Hypervisor Operations', 'hypervisor@platform.internal'),
        ('nova-scheduler', 'Core Scheduling Team', 'scheduler@platform.internal'),
        ('KERNEL', 'Core OS Team', 'os-team@platform.internal'),
        ('APP', 'Application Runtime Team', 'apps@platform.internal'),
        ('HARDWARE', 'Data Center Ops', 'dc-ops@platform.internal');
    """
    run_cmd(f'docker exec -i serenedb serened psql -p 7890 -c "{schema_sql}"')
    
    # 2. Copy and Ingest OpenStack (download on demand if missing)
    ensure_dataset("openstack")
    for f in ["openstack_normal1.log", "openstack_normal2.log", "openstack_abnormal.log"]:
        if os.path.exists(f):
            run_cmd(f"docker cp .\\{f} serenedb:/tmp/{f}")
            # Real OpenStack line layout (space-separated):
            #   [1]=logfile token (e.g. "nova-api.log.1.2017-05-16_13:53:08")
            #   [2]=date  [3]=time.ms  [4]=pid  [5]=LEVEL  [6]=module path  [7:]=content
            # The component that matches team_ownership (nova-api, nova-compute, ...)
            # is the prefix of the logfile token before ".log", so derive it there.
            ingest_sql = f"""
            INSERT INTO openstack (log_date, log_time, pid, level, component, content)
            SELECT 
                str_split(column0, ' ')[2], str_split(column0, ' ')[3], str_split(column0, ' ')[4],
                str_split(column0, ' ')[5],
                str_split(str_split(column0, ' ')[1], '.log')[1],
                array_to_string(str_split(column0, ' ')[7:], ' ')
            FROM read_csv('/tmp/{f}', header=false, sep='\\n', quote='', escape='', auto_detect=false, strict_mode=false, ignore_errors=true, columns={{'column0': 'VARCHAR'}})
            WHERE trim(column0) != '';
            """
            run_cmd(f'docker exec -i serenedb serened psql -p 7890 -c "{ingest_sql}"')

    # 3. Copy and Ingest BGL (download on demand if missing)
    ensure_dataset("bgl")
    bgl_file = "BGL_dataset/BGL.log" if os.path.exists("BGL_dataset/BGL.log") else "BGL.log"
    if os.path.exists(bgl_file):
        run_cmd(f"docker cp {bgl_file} serenedb:/tmp/BGL.log")
        bgl_sql = """
        INSERT INTO bgl (alert_flag, timestamp, date_str, node, time_str, node_repeat, log_type, component, level, content)
        SELECT 
            str_split(column0, ' ')[1], TRY_CAST(str_split(column0, ' ')[2] AS BIGINT), str_split(column0, ' ')[3],
            str_split(column0, ' ')[4], str_split(column0, ' ')[5], str_split(column0, ' ')[6],
            str_split(column0, ' ')[7], str_split(column0, ' ')[8], str_split(column0, ' ')[9],
            array_to_string(str_split(column0, ' ')[10:], ' ')
        FROM read_csv('/tmp/BGL.log', header=false, sep='\\n', quote='', escape='', auto_detect=false, strict_mode=false, ignore_errors=true, columns={'column0': 'VARCHAR'})
        WHERE trim(column0) != '' LIMIT 100000;
        """
        run_cmd(f'docker exec -i serenedb serened psql -p 7890 -c "{bgl_sql}"')

    print("=== Verification ===")
    run_cmd('docker exec -i serenedb serened psql -p 7890 -c "SELECT count(pid) FROM openstack;"')
    run_cmd('docker exec -i serenedb serened psql -p 7890 -c "SELECT count(component) FROM bgl;"')

if __name__ == "__main__":
    main()