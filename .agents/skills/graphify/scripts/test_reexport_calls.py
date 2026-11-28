"""Regression for the tablet's relative import of an aliased function."""
import ast
import unittest

from reexport_calls import add_reexport_calls


class ReexportCallTests(unittest.TestCase):
    def test_resolves_only_the_called_global(self):
        sources = {
            "trips/views.py": "def board(): pass\n",
            "other/views.py": "def board(): pass\n",
            "assignments/views.py": "from trips.views import board as render\n",
            "assignments/tablet.py": (
                "from .views import render as dashboard\n"
                "def tablet():\n    return dashboard()\n"
                "def local(dashboard):\n    return dashboard()\n"
                "def callback():\n    return dashboard\n"
                "def nested():\n    return lambda: dashboard()\n"
            ),
            "assignments/rebound.py": "from trips.views import board as render\nrender = print\n",
            "assignments/bad.py": "from .rebound import render\ndef bad(): return render()\n",
            "assignments/a.py": "from .b import render\ndef cyclic(): return render()\n",
            "assignments/b.py": "from .a import render\n",
        }
        nodes = [
            {"id": path + ":" + fn.name, "source_file": path, "source_location": f"L{fn.lineno}",
             "label": fn.name + "()", "_callable": True}
            for path, source in sources.items() for fn in ast.parse(source).body
            if isinstance(fn, ast.FunctionDef)
        ]
        graph = {"nodes": nodes, "links": []}
        self.assertEqual(add_reexport_calls(graph, sources), 1)
        edge, = graph["links"]
        self.assertEqual((edge["source"], edge["target"], edge["relation"], edge["source_location"]),
                         ("assignments/tablet.py:tablet", "trips/views.py:board", "calls", "L3"))
        self.assertEqual(add_reexport_calls(graph, sources), 0)


if __name__ == "__main__":
    unittest.main()
