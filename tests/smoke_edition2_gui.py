"""Real Qt/WebEngine integration check; writes only to temporary/build-test paths."""
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "windows"))
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication, QPushButton
from autocf_desktop_v2 import AutoCFWindow, role_feature
from map_export import SNAPSHOT_JS


def run():
    output = ROOT / ".codex-build-portable" / "checks"
    output.mkdir(exist_ok=True)
    app = QApplication([])
    errors = []
    def wait(condition, seconds=60):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            app.processEvents()
            if errors:
                raise AssertionError(errors)
            if condition():
                return
            time.sleep(.02)
        raise AssertionError("Timed out waiting for Qt/WebEngine")
    def warning(*args, **kwargs):
        errors.append(str(args[1:]))
    with tempfile.TemporaryDirectory() as folder, patch("autocf_desktop_v2.QMessageBox.warning", warning), \
            patch("autocf_desktop_v2.QMessageBox.critical", warning):
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, folder)
        window = AutoCFWindow()
        window.move(-10000, 0)
        window.resize(1700, 1000); window.show(); window.go_to("study")
        print("Window initialized; WebEngine:", window.study_map.web, flush=True)
        try:
            wait(lambda: window.study_map.loaded, 25)
        except AssertionError:
            window.study_map.web.page().runJavaScript(
                "JSON.stringify({ready:document.readyState,html:document.documentElement.outerHTML.slice(-1500),errors:window.__acfErrors})",
                lambda value: print("MAP DIAGNOSTIC:", value, flush=True))
            until = time.monotonic()+2
            while time.monotonic()<until: app.processEvents(); time.sleep(.02)
            raise
        assert not any(b.text().startswith("Open NASA Delta-X download") for b in window.findChildren(QPushButton))
        domain = role_feature("domain", {"type":"Polygon", "coordinates":[[[-2.98,53.96],[-2.78,54.06],[-2.74,54.00],[-2.94,53.91],[-2.98,53.96]]]}, {})
        ocean = role_feature("ocean_boundary", {"type":"LineString", "coordinates":[[-2.98,53.96],[-2.94,53.91]]}, {})
        def point(role, id, lon, lat):
            return role_feature(role, {"type":"Point", "coordinates":[lon,lat]}, {"ID":id,"name":id})
        document = {"type":"FeatureCollection", "features":[domain, ocean,
            point("ocean_forcing_point","bnd001",-2.97,53.95),
            point("observation_point","obs001",-2.84,54.00)]}
        window.configure_model_workspace(Path(folder) / "model", "model")
        for index in (3,4,5):
            window.add_terrain_entry(dict(source="file", path=f"./source{index}.tif",
                                          vertical_datum="ODN", output_resolution_m=index*10))
        window.remove_terrain_entry(window.extra_terrain_entries[-1])
        study = Path(folder) / "model" / "model_study_area.geojson"
        AutoCFWindow.atomic_write_text(study, json.dumps(document))
        window.geojson_path.setText(str(study))
        window.study_map.set_draft(document)
        window.study_map.set_basemap("Esri World Imagery")
        window.study_map.run("map.jumpTo({center:[-2.86,54.0],zoom:10,bearing:0,pitch:0});")
        window.ocean_source.setCurrentText("file")
        window.river_source.setCurrentText("file")
        window.go_to("forcing")
        window.forcing_tabs.setCurrentIndex(2)
        imported = Path(folder) / "river.json"
        imported.write_text(json.dumps({"type":"FeatureCollection", "features":[point("river_source","1",-2.80,54.02)]}))
        with patch("autocf_desktop_v2.QFileDialog.getOpenFileName", return_value=(str(imported), "")):
            window.import_point_locations("river", "river_source")
        wait(lambda: not window._forcing_save_pending)
        def points(role):
            return [f for f in json.loads(study.read_text())["features"] if f["properties"]["role"] == role]
        assert points("river_source")[0]["properties"]["ID"] == "1"
        assert window.page_keys[window.navigation.currentRow()] == "forcing"
        assert len(list((Path(folder) / "model").glob("*.yml"))) == 1
        assert len(list((Path(folder) / "model").glob("*.geojson"))) == 1
        ocean_import = Path(folder) / "ocean.json"
        ocean_import.write_text(json.dumps({"type":"FeatureCollection", "features":[
            point("ocean_forcing_point","bnd002",-2.97,53.95)]}))
        with patch("autocf_desktop_v2.QFileDialog.getOpenFileName", return_value=(str(ocean_import), "")):
            window.import_point_locations("ocean", "ocean_forcing_point")
        wait(lambda: not window._forcing_save_pending)
        assert points("ocean_forcing_point")[0]["properties"]["ID"] == "bnd002"
        assert len(points("river_source")) == 1
        editor = window.coordinate_editors["river"]
        editor["table"].selectRow(0)
        editor["lat"].setText("54.025"); editor["lon"].setText("-2.805")
        window.add_coordinate_point("river")
        wait(lambda: not window._forcing_save_pending)
        assert points("river_source")[0]["geometry"]["coordinates"] == [-2.805,54.025]
        window.delete_coordinate_point("river")
        wait(lambda: not window._forcing_save_pending)
        assert not points("river_source")
        # A map-click uses the same JS add path, caught by the revision observer.
        window.study_map.run("AutoCF.add('river_source',{type:'Point',coordinates:[-2.8,54.02]},{ID:'1',name:'River'});")
        wait(lambda: len(points("river_source")) == 1)
        window.load_config(window.config_path)
        assert len(window.extra_terrain_entries) == 2
        assert [e["path"].text() for e in window.extra_terrain_entries] == ["./source3.tif", "./source4.tif"]
        assert [e["resolution"].value() for e in window.extra_terrain_entries] == [30,40]
        assert len(points("observation_point")) == 1
        window.go_to("forcing"); window.forcing_tabs.setCurrentIndex(2)
        window.study_map.run("map.jumpTo({center:[-2.86,53.99],zoom:10,bearing:0,pitch:0});")
        wait(lambda: window.coordinate_editors["river"]["table"].rowCount() == 1)
        received = []
        window.study_map.fetch(SNAPSHOT_JS, received.append)
        wait(lambda: bool(received))
        png = output / "map-export-300dpi.png"
        window.export_map_button.setEnabled(False)
        window._start_map_export(received[0], 10, str(png))
        wait(lambda: window.export_map_button.isEnabled())
        image = QImage(str(png))
        assert image.width() == 3000, (image.width(),image.height())
        assert abs(image.dotsPerMeterX()*.0254-300) < .1
        after = []
        window.study_map.fetch(SNAPSHOT_JS, after.append); wait(lambda: bool(after))
        for key in ("center", "zoom", "bearing", "pitch"):
            assert json.loads(received[0])[key] == json.loads(after[0])[key], key
        window.dirty = False; window.close()
        print(json.dumps({"passed": ["NASA button removed","ocean/river GeoJSON imports autosaved","coordinate edit autosaved",
              "delete autosaved","map placement autosaved","single YAML/GeoJSON","reopen","300dpi PNG",
              "add/remove terrain entries and reopen ordered sources 3/4"],
              "png":str(png),"size":[image.width(),image.height()]}, indent=2), flush=True)


if __name__ == "__main__":
    run()
