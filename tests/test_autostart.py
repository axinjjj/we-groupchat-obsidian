import plistlib
import tempfile
from pathlib import Path
import unittest

from scripts.autostart import app_bundle_executable, build_plist


class AutostartAppBundleTests(unittest.TestCase):
    def make_app(self, root: Path) -> Path:
        app = root / "dist" / "WeGroupchatObsidian.app"
        contents = app / "Contents"
        executable = contents / "MacOS" / "WeGroupchatObsidian"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n", encoding="utf-8")
        with (contents / "Info.plist").open("wb") as handle:
            plistlib.dump({"CFBundleExecutable": "WeGroupchatObsidian"}, handle)
        return app

    def test_app_bundle_executable_reads_info_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.make_app(Path(tmp))

            executable = app_bundle_executable(app)

        self.assertEqual(executable.name, "WeGroupchatObsidian")
        self.assertEqual(executable.parent.name, "MacOS")

    def test_build_plist_can_run_app_bundle_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = self.make_app(root)

            plist = build_plist(
                "io.github.indeliblevivi.we-groupchat-obsidian",
                app_bundle=app,
                project_dir=root,
            )

        self.assertEqual(
            plist["ProgramArguments"][0],
            str((app / "Contents/MacOS/WeGroupchatObsidian").resolve()),
        )
        self.assertEqual(plist["ProgramArguments"][1], "--autostart")
        self.assertEqual(plist["WorkingDirectory"], str(root.resolve()))
        self.assertEqual(plist["EnvironmentVariables"]["WE_GROUPCHAT_OBSIDIAN_AUTOSTART"], "1")


if __name__ == "__main__":
    unittest.main()
