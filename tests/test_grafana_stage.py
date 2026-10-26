from app.tools import grafana

def test_dashboard_uses_discovered_metric_labels():
    info = {"family": "custom_calls_total", "service_label": "workload", "status_label": "status", "kind_label": "kind", "dur_family": None}
    dash = grafana.build_dashboard(info, "platform-a", "dashboard-a", set(), "prom-a")
    text = str(dash)
    assert "custom_calls_total" in text
    assert "workload" in text
    assert "prom-a" in text
    assert dash["uid"] == "dashboard-a"
