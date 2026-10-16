import subprocess
import json
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("LogInsightsSereneDB")

def run_serene_query(sql: str) -> str:
    """Executes SQL against SereneDB via docker exec and returns standard output."""
    cmd = [
        "docker", "exec", "-i", "serenedb",
        "serened", "psql", "-p", "7890", "-c", sql
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout.strip()

@mcp.tool()
def search_error_logs(table_name: str, keyword: str, limit: int = 5) -> str:
    """
    Role: Engineer
    Searches logs for specific error keywords or severity levels.
    Returns matching log entries with timestamps, components, and messages.
    """
    clean_table = "openstack" if "openstack" in table_name.lower() else "bgl"
    # Basic sanitization
    clean_kw = keyword.replace("'", "''")
    
    if clean_table == "openstack":
        sql = f"""
            SELECT log_date, log_time, pid, level, component, content 
            FROM openstack 
            WHERE content ILIKE '%{clean_kw}%' OR level ILIKE '%{clean_kw}%' 
            LIMIT {limit};
        """
    else:
        sql = f"""
            SELECT date_str, time_str, node, level, component, content 
            FROM bgl 
            WHERE content ILIKE '%{clean_kw}%' OR level ILIKE '%{clean_kw}%' 
            LIMIT {limit};
        """
    return run_serene_query(sql)

@mcp.tool()
def get_nearby_events(table_name: str, target_time: str, limit: int = 6) -> str:
    """
    Role: Engineer
    Retrieves chronological log events around a specific timestamp to inspect
    what occurred immediately before and after an error.
    """
    clean_table = "openstack" if "openstack" in table_name.lower() else "bgl"
    clean_time = target_time.replace("'", "''")
    
    if clean_table == "openstack":
        sql = f"""
            SELECT log_date, log_time, level, component, content 
            FROM openstack 
            WHERE log_time >= '{clean_time}' 
            ORDER BY log_time ASC 
            LIMIT {limit};
        """
    else:
        sql = f"""
            SELECT date_str, time_str, level, component, content 
            FROM bgl 
            WHERE time_str >= '{clean_time}' 
            ORDER BY time_str ASC 
            LIMIT {limit};
        """
    return run_serene_query(sql)

@mcp.tool()
def get_recurring_issues_by_team(table_name: str = "openstack") -> str:
    """
    Role: Team Lead
    Aggregates whole-corpus error counts grouped by component, joined with 
    the team-ownership table to report which team is responsible.
    """
    clean_table = "openstack" if "openstack" in table_name.lower() else "bgl"
    sql = f"""
        SELECT 
            l.component, 
            COALESCE(t.team_name, 'Unassigned / Core Infrastructure') AS team_name, 
            COALESCE(t.lead_contact, 'N/A') AS lead_contact, 
            COUNT(l.component) AS total_failures
        FROM {clean_table} l
        LEFT JOIN team_ownership t ON l.component = t.component
        WHERE l.level IN ('ERROR', 'CRITICAL', 'FATAL', 'WARNING')
        GROUP BY l.component, t.team_name, t.lead_contact
        ORDER BY total_failures DESC;
    """
    return run_serene_query(sql)

@mcp.tool()
def get_risk_distribution(table_name: str = "openstack") -> str:
    """
    Role: Manager
    Calculates whole-corpus distribution of severity levels across the dataset
    to establish measured frequencies without hallucinated incident figures.
    """
    clean_table = "openstack" if "openstack" in table_name.lower() else "bgl"
    sql = f"""
        SELECT level, COUNT(level) AS count
        FROM {clean_table}
        GROUP BY level
        ORDER BY count DESC;
    """
    return run_serene_query(sql)

if __name__ == "__main__":
    mcp.run()