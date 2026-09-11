from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import tempfile

import pytest

from scripts.patch_camera_controls import patch_camera_controls, replace_once


PINNED_SOURCE = Path(tempfile.gettempdir()) / "flow-controller-camera-runtime" / "source"


def _copy_camera_patch_inputs(destination: Path) -> None:
    inputs = (
        Path("Canon.Eos.Framework/EosCamera.cs"),
        Path("CameraControl.Devices/Classes/PropertyValue.cs"),
        Path("CameraControl.Devices/Canon/CanonSDKBase.cs"),
    )
    if not PINNED_SOURCE.is_dir():
        pytest.skip("pinned digiCamControl source cache is not available")
    for relative in inputs:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PINNED_SOURCE / relative, target)


def test_camera_control_patch_applies_to_pinned_source(tmp_path: Path) -> None:
    _copy_camera_patch_inputs(tmp_path)

    patch_camera_controls(tmp_path)

    eos_camera = (tmp_path / "Canon.Eos.Framework/EosCamera.cs").read_text()
    assert "for (int attempt = 0; attempt < 11; attempt++)" in eos_camera
    assert "this.SetPropertyIntegerData(propertyId, val);\n                        return;" in eos_camera
    assert "exception.EosErrorCode == EosErrorCode.DeviceBusy" in eos_camera
    assert "exception.EosErrorCode == EosErrorCode.ObjectNotReady" in eos_camera
    assert "if (!transient || attempt == 10)\n                            throw;" in eos_camera
    assert eos_camera.count("Thread.Sleep(50);") == 1
    assert "bool retry = false;" not in eos_camera

    property_value = (
        tmp_path / "CameraControl.Devices/Classes/PropertyValue.cs"
    ).read_text()
    sync_method = property_value.index("public void SetValueSynchronously(string value)")
    async_method = property_value.index("public void OnValueChanged(object sender", sync_method)
    sync_block = property_value[sync_method:async_method]
    assert "_synchronousValueChange = true;" in sync_block
    assert "catch\n                {\n                    HaveError = true;\n                    throw;" in sync_block
    assert (
        "finally\n                {\n"
        "                    _notifyValuChange = true;\n"
        "                    _synchronousValueChange = false;"
    ) in sync_block
    assert "if (_synchronousValueChange)" in property_value
    assert "OnValueChangedThread(new object[] { sender, key, val });\n                return;" in property_value
    assert "else if (_synchronousValueChange)\n                        {\n                            throw;" in property_value

    sdk = (tmp_path / "CameraControl.Devices/Canon/CanonSDKBase.cs").read_text()
    iso_start = sdk.index("private void IsoNumber_ValueChanged")
    iso_end = sdk.index("public List<int> GetSettingsList", iso_start)
    iso = sdk[iso_start:iso_end]
    assert iso.index("Camera.SetProperty") < iso.index("Camera.GetProperty")
    assert "IsoNumber.SetValue(actual, false);" in iso
    assert "if (actual != val)" in iso
    assert "IsoNumber.HaveError = true;" in iso
    assert "if (IsoNumber.IsSynchronousValueChange)\n                    throw;" in iso
    assert "finally\n            {\n                try\n                {\n                    Camera.ResumeLiveview();" in iso
    assert 'Log.Debug("Error resume live view after ISO change", exception);' in iso

    pointer_start = sdk.index('Log.Debug("Pointer file transfer started")')
    pointer_end = sdk.index("EosFileImageEventArgs file", pointer_start)
    pointer_transfer = sdk[pointer_start:pointer_end]
    assert "File.Delete(filename);\n                        throw;" in pointer_transfer
    assert "finally\n                    {\n                        Camera.ResumeLiveview();" in pointer_transfer

    assert "private volatile bool _keepAliveRequested;" in sdk
    keep_alive = _extract_csharp_method(sdk, "        public void KeepAlive()")
    assert "!PreventShutDown || !IsConnected || Camera == null || IsBusy" in keep_alive
    assert "ErrorCodes.GetCanonException(Camera.SendCommand(" in keep_alive
    assert "_keepAliveRequested = false;" in keep_alive
    will_shutdown = _extract_csharp_method(
        sdk, "        private void Camera_WillShutdown(object sender, EventArgs e)"
    )
    assert will_shutdown.count("_keepAliveRequested = true;") == 1
    assert "SendCommand" not in will_shutdown


def test_camera_control_patch_rejects_already_patched_source(tmp_path: Path) -> None:
    _copy_camera_patch_inputs(tmp_path)
    patch_camera_controls(tmp_path)

    with pytest.raises(RuntimeError, match="expected once, found 0"):
        patch_camera_controls(tmp_path)


def test_replace_once_rejects_missing_and_duplicate_fragments() -> None:
    with pytest.raises(RuntimeError, match="expected once, found 0"):
        replace_once("abc", "missing", "new", "missing fragment")
    with pytest.raises(RuntimeError, match="expected once, found 2"):
        replace_once("old old", "old", "new", "duplicate fragment")


def test_runtime_builder_applies_and_records_camera_patches() -> None:
    builder = (
        Path(__file__).resolve().parents[1] / "scripts" / "build_camera_runtime.py"
    ).read_text(encoding="utf-8")

    assert builder.index("patch_manager(staging)") < builder.index(
        "patch_camera_controls(staging)"
    )
    for name in (
        "Canon property writes retry only transient errors and propagate final failure",
        "PropertyValue supports verified synchronous value changes",
        "Canon ISO writes use readback verification and restore live view",
        "Canon pointer transfers restore live view and propagate failure",
        "Canon shutdown events request owner-thread keep-alive with checked native errors",
    ):
        assert f'"{name}"' in builder


def _extract_csharp_method(source: str, signature: str) -> str:
    start = source.index(signature)
    opening_brace = source.index("{", start)
    depth = 0
    for index in range(opening_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"Unclosed C# method: {signature}")


@pytest.fixture(scope="module")
def set_property_harness(tmp_path_factory: pytest.TempPathFactory) -> Path:
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    compiler = windows / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    if not PINNED_SOURCE.is_dir() or not compiler.is_file():
        pytest.skip("pinned source cache or Windows .NET Framework compiler unavailable")

    workspace = tmp_path_factory.mktemp("set-property-harness")
    _copy_camera_patch_inputs(workspace)
    patch_camera_controls(workspace)
    eos_source = (workspace / "Canon.Eos.Framework/EosCamera.cs").read_text()
    method = _extract_csharp_method(
        eos_source, "        public void SetProperty(uint propertyId, long val)"
    )
    harness_source = """using System;
using System.Threading;

public enum EosErrorCode
{
    DeviceBusy,
    ObjectNotReady,
    Permanent
}

public sealed class EosPropertyException : Exception
{
    public EosErrorCode EosErrorCode { get; private set; }

    public EosPropertyException(EosErrorCode code)
    {
        EosErrorCode = code;
    }
}

public static class Edsdk
{
    public const uint CameraCommand_DoEvfAf = 1;
}

public sealed class Camera
{
    private readonly object _locker = new object();
    private readonly string _scenario;
    public int Calls { get; private set; }

    public Camera(string scenario)
    {
        _scenario = scenario;
    }

    private void SendCommand(uint command, int value) { }

    private void SetPropertyIntegerData(uint propertyId, long value)
    {
        Calls++;
        if (_scenario == "busy-success" && Calls == 1)
            throw new EosPropertyException(EosErrorCode.DeviceBusy);
        if (_scenario == "permanent")
            throw new EosPropertyException(EosErrorCode.Permanent);
        if (_scenario == "persistent-busy")
            throw new EosPropertyException(EosErrorCode.DeviceBusy);
    }

METHOD
}

public static class Program
{
    public static int Main(string[] args)
    {
        string scenario = args[0];
        var camera = new Camera(scenario);
        try
        {
            camera.SetProperty(1, 100);
            if (scenario == "busy-success" && camera.Calls == 2)
            {
                Console.WriteLine("returned:2");
                return 0;
            }
            Console.Error.WriteLine("unexpected return after " + camera.Calls + " calls");
            return 2;
        }
        catch (EosPropertyException)
        {
            int expected = scenario == "permanent" ? 1 : 11;
            if (camera.Calls == expected)
            {
                Console.WriteLine("threw:" + camera.Calls);
                return 0;
            }
            Console.Error.WriteLine("unexpected throw after " + camera.Calls + " calls");
            return 3;
        }
    }
}
""".replace("METHOD", method)
    source_path = workspace / "SetPropertyHarness.cs"
    executable = workspace / "SetPropertyHarness.exe"
    source_path.write_text(harness_source, encoding="utf-8")
    compile_result = subprocess.run(
        [str(compiler), "/nologo", f"/out:{executable}", str(source_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr
    return executable


@pytest.mark.parametrize(
    ("scenario", "expected"),
    (
        ("busy-success", "returned:2"),
        ("permanent", "threw:1"),
        ("persistent-busy", "threw:11"),
    ),
)
def test_patched_set_property_executable_behavior(
    set_property_harness: Path, scenario: str, expected: str
) -> None:
    result = subprocess.run(
        [str(set_property_harness), scenario],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == expected


@pytest.fixture(scope="module")
def canon_keep_alive_harness(tmp_path_factory: pytest.TempPathFactory) -> Path:
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    compiler = windows / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    if not PINNED_SOURCE.is_dir() or not compiler.is_file():
        pytest.skip("pinned source cache or Windows .NET Framework compiler unavailable")

    workspace = tmp_path_factory.mktemp("canon-keep-alive-harness")
    _copy_camera_patch_inputs(workspace)
    patch_camera_controls(workspace)
    sdk_source = (
        workspace / "CameraControl.Devices/Canon/CanonSDKBase.cs"
    ).read_text()
    keep_alive = _extract_csharp_method(sdk_source, "        public void KeepAlive()")
    will_shutdown = _extract_csharp_method(
        sdk_source, "        private void Camera_WillShutdown(object sender, EventArgs e)"
    )
    harness_source = """using System;

public static class Edsdk
{
    public const uint CameraCommand_ExtendShutDownTimer = 99;
}

public static class ErrorCodes
{
    public static void GetCanonException(uint code)
    {
        if (code != 0)
            throw new InvalidOperationException("Canon error " + code);
    }
}

public sealed class FakeCamera
{
    public uint Result;
    public int Calls;
    public uint LastCommand;

    public uint SendCommand(uint command)
    {
        Calls++;
        LastCommand = command;
        return Result;
    }
}

public sealed class CanonSDKBase
{
    private volatile bool _keepAliveRequested;
    public bool PreventShutDown = true;
    public bool IsConnected = true;
    public bool IsBusy;
    public FakeCamera Camera = new FakeCamera();
    public bool Requested { get { return _keepAliveRequested; } }

KEEP_ALIVE

WILL_SHUTDOWN

    public void TriggerWarning()
    {
        Camera_WillShutdown(null, EventArgs.Empty);
    }
}

public static class Program
{
    private static int Fail(string message)
    {
        Console.Error.WriteLine(message);
        return 2;
    }

    public static int Main(string[] args)
    {
        string scenario = args[0];
        var sdk = new CanonSDKBase();
        sdk.TriggerWarning();
        if (!sdk.Requested || sdk.Camera.Calls != 0)
            return Fail("warning callback performed I/O or did not set the flag");

        if (scenario == "disabled")
            sdk.PreventShutDown = false;
        else if (scenario == "disconnected")
            sdk.IsConnected = false;
        else if (scenario == "busy")
            sdk.IsBusy = true;
        else if (scenario == "null-camera")
            sdk.Camera = null;
        else if (scenario == "error")
            sdk.Camera.Result = 5;

        try
        {
            sdk.KeepAlive();
            if (scenario == "error")
                return Fail("nonzero Canon result did not throw");
        }
        catch (InvalidOperationException)
        {
            if (scenario != "error")
                return Fail("unexpected Canon exception");
            if (!sdk.Requested || sdk.Camera.Calls != 1)
                return Fail("error cleared flag or used wrong call count");
            Console.WriteLine("error-retained");
            return 0;
        }

        if (scenario == "accepted")
        {
            if (sdk.Requested || sdk.Camera.Calls != 1 ||
                sdk.Camera.LastCommand != Edsdk.CameraCommand_ExtendShutDownTimer)
                return Fail("accepted keep-alive state mismatch");
            Console.WriteLine("accepted");
            return 0;
        }

        if (!sdk.Requested)
            return Fail("skipped keep-alive cleared request flag");
        int calls = sdk.Camera == null ? 0 : sdk.Camera.Calls;
        if (calls != 0)
            return Fail("skipped keep-alive performed I/O");
        Console.WriteLine("skipped");
        return 0;
    }
}
""".replace("KEEP_ALIVE", keep_alive).replace("WILL_SHUTDOWN", will_shutdown)
    source_path = workspace / "CanonKeepAliveHarness.cs"
    executable = workspace / "CanonKeepAliveHarness.exe"
    source_path.write_text(harness_source, encoding="utf-8")
    compile_result = subprocess.run(
        [str(compiler), "/nologo", f"/out:{executable}", str(source_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert compile_result.returncode == 0, compile_result.stdout + compile_result.stderr
    return executable


@pytest.mark.parametrize(
    ("scenario", "expected"),
    (
        ("accepted", "accepted"),
        ("error", "error-retained"),
        ("disabled", "skipped"),
        ("disconnected", "skipped"),
        ("busy", "skipped"),
        ("null-camera", "skipped"),
    ),
)
def test_patched_canon_keep_alive_executable_behavior(
    canon_keep_alive_harness: Path, scenario: str, expected: str
) -> None:
    result = subprocess.run(
        [str(canon_keep_alive_harness), scenario],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == expected
