import platform_glue as glue


def test_launch_agent_restarts_only_after_a_crash():
    assert glue.launch_agent_plist()["KeepAlive"] == {"SuccessfulExit": False}


def test_launch_agent_runs_the_tray_app():
    assert glue.launch_agent_plist()["ProgramArguments"][-1].endswith("tray_app.py")
