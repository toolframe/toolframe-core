import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CORE_DIRECTORY = REPOSITORY_ROOT / "core"
if str(CORE_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(CORE_DIRECTORY))

import toolframe_architecture as architecture


class SourceArchitectureTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.app = self.root / "app"
        self.domains = self.root / "domains"
        self.app.mkdir()
        self.domains.mkdir()
        (self.app / "dashboard.py").write_text("pass\n", encoding="utf-8")
        self.sales = self.create_domain("sales")
        self.reporting = self.create_domain("reporting")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def create_domain(self, name: str) -> Path:
        domain = self.domains / name
        domain.mkdir()
        (domain / "__init__.py").write_text("\n", encoding="utf-8")
        for child in ("llms", "model", "tools", "ui", "migrations"):
            (domain / child).mkdir()
        return domain

    def validate(self) -> None:
        architecture.validate_source_architecture(self.app, self.domains)

    def test_allows_same_domain_internals_and_explicit_cross_domain_api(self):
        (self.sales / "__init__.py").write_text(
            "from .model.revenue import revenue_total\n", encoding="utf-8"
        )
        (self.sales / "model" / "revenue.py").write_text(
            "def revenue_total():\n    return 0\n", encoding="utf-8"
        )
        (self.reporting / "model" / "summary.py").write_text(
            "from domains.sales import revenue_total\n"
            "from domains.reporting.model import local_summary\n",
            encoding="utf-8",
        )
        (self.reporting / "model" / "local_summary.py").write_text(
            "def local_summary():\n    return None\n", encoding="utf-8"
        )
        (self.app / "dashboard.py").write_text(
            "from domains.sales import revenue_total\n", encoding="utf-8"
        )

        self.validate()

    def test_rejects_deep_cross_domain_imports_from_every_public_surface(self):
        locations = (
            self.reporting / "__init__.py",
            self.reporting / "model" / "summary.py",
            self.reporting / "tools" / "summary.py",
            self.reporting / "ui" / "summary.py",
        )
        for path in locations:
            with self.subTest(path=path):
                path.write_text(
                    "from domains.sales.model.revenue import revenue_total\n",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    architecture.SourceArchitectureError,
                    "deep cross-domain imports are forbidden",
                ):
                    self.validate()
                path.write_text("\n", encoding="utf-8")

    def test_does_not_restrict_how_dashboard_composes_domains(self):
        (self.app / "dashboard.py").write_text(
            "import domains.sales.model.revenue\n"
            "from domains.reporting.ui.summary import render\n",
            encoding="utf-8",
        )

        self.validate()

    def test_rejects_cross_domain_package_wildcard_and_internal_imports(self):
        rejected = (
            "import domains.sales\n",
            "from domains.sales import *\n",
            "from domains.sales import model\n",
            "from domains.unknown import calculate\n",
        )
        path = self.reporting / "model" / "summary.py"
        for source in rejected:
            with self.subTest(source=source):
                path.write_text(source, encoding="utf-8")
                with self.assertRaises(architecture.SourceArchitectureError):
                    self.validate()

    def test_resolves_relative_imports_before_enforcing_the_boundary(self):
        nested = self.reporting / "model" / "nested"
        nested.mkdir()
        source = nested / "summary.py"
        source.write_text(
            "from ....sales.model.revenue import revenue_total\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            architecture.SourceArchitectureError,
            "deep cross-domain imports are forbidden",
        ):
            self.validate()

    def test_compiles_public_sources_but_does_not_execute_them(self):
        (self.reporting / "model" / "unused.py").write_text(
            "raise RuntimeError('must not execute')\n", encoding="utf-8"
        )
        (self.reporting / "tools" / "_private.py").write_text("if:\n", encoding="utf-8")
        for source_directory in ("tools", "ui"):
            ignored = self.reporting / source_directory / "nested"
            ignored.mkdir()
            (ignored / "broken.py").write_text("if:\n", encoding="utf-8")

        self.validate()

        (self.reporting / "model" / "broken.py").write_text("if:\n", encoding="utf-8")
        with self.assertRaisesRegex(
            architecture.SourceArchitectureError,
            "failed compile validation.*broken.py",
        ):
            self.validate()

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks are unavailable")
    def test_rejects_public_source_symlinks(self):
        target = self.root / "outside.py"
        target.write_text("pass\n", encoding="utf-8")
        (self.reporting / "model" / "linked.py").symlink_to(target)

        with self.assertRaisesRegex(
            architecture.SourceArchitectureError,
            "public Python source path must not be a symlink",
        ):
            self.validate()


if __name__ == "__main__":
    unittest.main()
