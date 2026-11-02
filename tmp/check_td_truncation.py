"""Tighten only the CST TD residual on already solved FD geometries."""
from pathlib import Path
import sys,json,shutil
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parent))
import run_qtem_checks as em
from run_directional_fd_checks import goal
from eaopt.config import CaseConfig
from eaopt.solver.cst import _connect,_open_or_reuse,_save,_run_solver

def main():
    de=_connect(False,True,em.log,quiet=True)
    report={}
    try:
        for case,direction in [('divider','upper'),('coupler','left')]:
            cfg=CaseConfig.from_yaml(em.ROOT/f'configs/{case}.yaml')
            root=em.OUT/case/'fd_fine'
            values={}
            for sign in ('plus','minus'):
                source=root/f'{direction}_0.04_{sign}'
                target=root/'td60'/source.name
                target.mkdir(parents=True,exist_ok=True)
                assert (source/'complete.json').exists()
                code='With Solver\n .SteadyStateLimit "-60"\nEnd With'
                em.write(target/'settings.json',{'source':str(source/'model.cst'),
                    'source_configuration':json.loads((source/'config.json').read_text(encoding='utf-8')),
                    'only_update':code})
                if not (target/'complete.json').exists():
                    path=target/'model.cst'
                    project=_open_or_reuse(de,path if path.exists() else source/'model.cst',em.log)
                    try:
                        if not path.exists():
                            _save(project,path,em.log,what='Copy fixed geometry for TD residual check')
                        project.model3d.add_to_history('TD residual diagnostic',code)
                        _save(project,path,em.log)
                        seconds=_run_solver(project.model3d,em.log)
                        _save(project,path,em.log)
                        em.write(target/'s_params.json',em.read_results(path,1))
                        for name in ('Model.msh','Model.log','MCalc.log'):
                            shutil.copy2(path.with_suffix('')/'Result'/name,target/name)
                        assert (target/'Model.msh').read_bytes()==(source/'Model.msh').read_bytes(), 'TD mesh changed'
                        em.write(target/'complete.json',{'seconds':seconds,'status':'complete'})
                    finally:
                        project.close()
                values[sign]=goal(target,cfg)
            new=(values['plus']-values['minus'])/.08
            old_data=json.loads((root/'directional_checks.json').read_text(encoding='utf-8'))
            old=next(r['finite_difference_per_mm'] for r in old_data['rows'] if r['direction']==direction and r['h_mm']==.04)
            report[case]={'direction':direction,'h_mm':.04,'TD40_FD':old,'TD60_FD':new,
                'relative_FD_change':abs(new-old)/max(abs(old),1e-15),'TD60_values':values}
            em.write(em.OUT/'td_truncation_checks.json',report)
            em.log(f'TD_TRUNCATION {case} {report[case]}')
    finally:
        de.close()

if __name__=='__main__':
    main()
