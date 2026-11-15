"""Reject wrong native solids while allowing actual high-vertex contours."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
import numpy as np

from eaopt.geometry.contour import MetalRegion
from eaopt.config import CaseConfig
from eaopt.solver import cst_model_antenna as model
from eaopt.solver.cst import CstSolver


class NativeGeometryAuditTests(unittest.TestCase):
    def test_audit_accounts_for_holes_and_records_actual_geometry(self):
        region=MetalRegion(np.array([[0,0],[2,0],[2,2],[0,2],[0,0]],float),
                           (np.array([[.5,.5],[1,.5],[1,1],[.5,1],[.5,.5]],float),))
        with tempfile.TemporaryDirectory() as folder:
            destination=Path(folder)/'native.json'
            backend=Mock()
            backend._execute_vba_code.side_effect=lambda _: destination.with_suffix('.txt').write_text(
                '0.0675\n16\n0\n2\n0\n2\n0\n0.018\n')
            rows=model.audit_design_regions(backend,[region],.018,destination)
            self.assertAlmostEqual(rows[0]['expected_volume_mm3'],.0675)
            self.assertTrue(json.loads(destination.read_text())['passed'])

    def test_wrong_volume_or_bounds_is_rejected_with_evidence(self):
        region=MetalRegion(np.array([[0,0],[2,0],[2,2],[0,2],[0,0]],float))
        for native in ('0.05\n8\n0\n2\n0\n2\n0\n0.018\n',
                       '0.072\n8\n0\n3\n0\n2\n0\n0.018\n'):
            with self.subTest(native=native),tempfile.TemporaryDirectory() as folder:
                destination=Path(folder)/'native.json'
                backend=Mock()
                backend._execute_vba_code.side_effect=lambda _:destination.with_suffix('.txt').write_text(native)
                with self.assertRaisesRegex(ValueError,'solid differs'):
                    model.audit_design_regions(backend,[region],.018,destination)
                self.assertFalse(json.loads(destination.read_text())['passed'])

    def test_incomplete_audit_cannot_reuse_a_previous_result(self):
        region=MetalRegion(np.array([[0,0],[2,0],[2,2],[0,2],[0,0]],float))
        with tempfile.TemporaryDirectory() as folder:
            destination=Path(folder)/'native.json'
            destination.with_suffix('.txt').write_text('0.072\n8\n0\n2\n0\n2\n0\n0.018\n')
            destination.write_text('{"passed": true}')
            with self.assertRaises(FileNotFoundError):
                model.audit_design_regions(Mock(),[region],.018,destination)
            self.assertFalse(json.loads(destination.read_text())['passed'])

    def test_large_antenna_contour_reaches_native_audit_without_simplification(self):
        root=Path(__file__).resolve().parents[1]
        cfg=CaseConfig.from_yaml(root/'configs/antenna.yaml')
        theta=np.linspace(0,2*np.pi,502)
        contour=np.column_stack((-10+5*np.cos(theta),5*np.sin(theta)))
        contour[-1]=contour[0]
        with tempfile.TemporaryDirectory() as folder:
            cfg.output.dir=folder
            solver=CstSolver(cfg,log=lambda _:None)
            solver.begin_iteration(0)
            project=Mock()
            with patch.object(solver,'_ensure_projects'),patch.object(solver,'_open',return_value=project), \
                    patch('eaopt.solver.cst._clear_results'),patch('eaopt.solver.cst._update_design_region') as updated, \
                    patch.object(model,'audit_design_regions') as audit:
                solver.build_model([contour])
            updated.assert_called_once()
            audit.assert_called_once()
            actual=updated.call_args.args[1][0].outer
            self.assertEqual(len(actual),len(contour))
            self.assertLess(np.max(np.abs(actual-contour)),1e-10)


if __name__=='__main__': unittest.main()
