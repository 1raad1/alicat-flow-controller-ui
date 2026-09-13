"""Apply Flow Controller's checked camera-control patches to pinned sources."""

from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    """Replace one exact upstream fragment, failing if the pin has drifted."""
    count = text.count(old)
    if count != 1:
        raise RuntimeError(
            f"Pinned source changed at camera control patch: {label} "
            f"(expected once, found {count})"
        )
    return text.replace(old, new)


def _patch_eos_camera(source: Path) -> None:
    path = source / "Canon.Eos.Framework" / "EosCamera.cs"
    text = path.read_text(encoding="utf-8-sig")
    old = """        public void SetProperty(uint propertyId, long val)
        {
            lock (_locker)
            {
                SendCommand(Edsdk.CameraCommand_DoEvfAf, 0);
                bool retry = false;
                int retrynum = 0;
                //DeviceReady();
                do
                {
                    if (retrynum > 10)
                    {
                        return;
                    }
                    try
                    {
                        this.SetPropertyIntegerData(propertyId, val);
                    }
                    catch (EosPropertyException)
                    {
                        Thread.Sleep(50);
                        retry = true;
                        retrynum++;
                    }
                } while (retry);
            }
        }
"""
    new = """        public void SetProperty(uint propertyId, long val)
        {
            lock (_locker)
            {
                SendCommand(Edsdk.CameraCommand_DoEvfAf, 0);
                for (int attempt = 0; attempt < 11; attempt++)
                {
                    try
                    {
                        this.SetPropertyIntegerData(propertyId, val);
                        return;
                    }
                    catch (EosPropertyException exception)
                    {
                        bool transient = exception.EosErrorCode == EosErrorCode.DeviceBusy ||
                                         exception.EosErrorCode == EosErrorCode.ObjectNotReady;
                        if (!transient || attempt == 10)
                            throw;
                        Thread.Sleep(50);
                    }
                }
            }
        }
"""
    path.write_text(
        replace_once(text, old, new, "bounded Canon property retry"),
        encoding="utf-8",
    )


def _patch_property_value(source: Path) -> None:
    path = source / "CameraControl.Devices" / "Classes" / "PropertyValue.cs"
    text = path.read_text(encoding="utf-8-sig")
    text = replace_once(
        text,
        "        private bool _notifyValuChange = true;\n"
        "        private readonly object _syncRoot = new object();",
        "        private bool _notifyValuChange = true;\n"
        "        private bool _synchronousValueChange;\n"
        "        private readonly object _syncRoot = new object();",
        "synchronous property-change field",
    )
    text = replace_once(
        text,
        """        public void OnValueChanged(object sender, string key, T val)
        {
            Thread thread = new Thread(OnValueChangedThread);
            thread.Name = "SetProperty thread " + Name;
            thread.Start(new object[] { sender, key, val });
            thread.Join(200);
        }
""",
        """        public bool IsSynchronousValueChange
        {
            get { return _synchronousValueChange; }
        }

        public void SetValueSynchronously(string value)
        {
            lock (_syncRoot)
            {
                _synchronousValueChange = true;
                try
                {
                    _notifyValuChange = true;
                    Value = value;
                }
                catch
                {
                    HaveError = true;
                    throw;
                }
                finally
                {
                    _notifyValuChange = true;
                    _synchronousValueChange = false;
                }
            }
        }

        public void OnValueChanged(object sender, string key, T val)
        {
            if (_synchronousValueChange)
            {
                OnValueChangedThread(new object[] { sender, key, val });
                return;
            }
            Thread thread = new Thread(OnValueChangedThread);
            thread.Name = "SetProperty thread " + Name;
            thread.Start(new object[] { sender, key, val });
            thread.Join(200);
        }
""",
        "synchronous property-change entry point",
    )
    text = replace_once(
        text,
        """                    catch (DeviceException exception)
                    {
                        if ((exception.ErrorCode == ErrorCodes.ERROR_BUSY ||
                             exception.ErrorCode == ErrorCodes.MTP_Device_Busy) && retrynum > 0)
                        {
                            retrynum--;
                            retry = true;
                            Thread.Sleep(100);
                        }
                    }
""",
        """                    catch (DeviceException exception)
                    {
                        if ((exception.ErrorCode == ErrorCodes.ERROR_BUSY ||
                             exception.ErrorCode == ErrorCodes.MTP_Device_Busy) && retrynum > 0)
                        {
                            retrynum--;
                            retry = true;
                            Thread.Sleep(100);
                        }
                        else if (_synchronousValueChange)
                        {
                            throw;
                        }
                    }
""",
        "synchronous device-error propagation",
    )
    path.write_text(text, encoding="utf-8")


def _patch_canon_sdk_base(source: Path) -> None:
    path = source / "CameraControl.Devices" / "Canon" / "CanonSDKBase.cs"
    text = path.read_text(encoding="utf-8-sig")
    text = replace_once(
        text,
        "        private System.Timers.Timer _shutdownTimer = new System.Timers.Timer(1000*60);\n",
        "        private System.Timers.Timer _shutdownTimer = new System.Timers.Timer(1000*60);\n"
        "        private volatile bool _keepAliveRequested;\n",
        "keep-alive request field",
    )
    text = replace_once(
        text,
        """        private void Camera_WillShutdown(object sender, EventArgs e)
        {
            try
            {
                if (PreventShutDown)
                {
                    Camera.SendCommand(Edsdk.CameraCommand_ExtendShutDownTimer);
                }
            }
            catch (Exception exception)
            {
                Log.Debug("PreventShutDown", exception);
            }
        }
""",
        """        public bool KeepAliveRequested
        {
            get { return _keepAliveRequested; }
        }

        public void KeepAlive()
        {
            if (!PreventShutDown || !IsConnected || Camera == null || IsBusy)
                return;
            ErrorCodes.GetCanonException(Camera.SendCommand(Edsdk.CameraCommand_ExtendShutDownTimer));
            _keepAliveRequested = false;
        }

        private void Camera_WillShutdown(object sender, EventArgs e)
        {
            _keepAliveRequested = true;
        }
""",
        "explicit owner-thread Canon keep-alive",
    )
    old_iso = """        private void IsoNumber_ValueChanged(object sender, string key, long val)
        {
            try
            {
                Camera.PauseLiveview();
                Camera.SetProperty(Edsdk.PropID_ISOSpeed, val);
                Camera.ResumeLiveview();
            }
            catch (Exception exception)
            {
                Log.Debug("Error set ISO to camera", exception);
            }
        }
"""
    new_iso = """        private void IsoNumber_ValueChanged(object sender, string key, long val)
        {
            try
            {
                Camera.PauseLiveview();
                Camera.SetProperty(Edsdk.PropID_ISOSpeed, val);
                long actual = Camera.GetProperty(Edsdk.PropID_ISOSpeed);
                IsoNumber.SetValue(actual, false);
                if (actual != val)
                    throw new InvalidOperationException("Camera did not accept the requested ISO value");
            }
            catch (Exception exception)
            {
                try
                {
                    long actual = Camera.GetProperty(Edsdk.PropID_ISOSpeed);
                    IsoNumber.SetValue(actual, false);
                }
                catch (Exception readbackException)
                {
                    Log.Debug("Error refresh ISO from camera", readbackException);
                }
                IsoNumber.HaveError = true;
                Log.Debug("Error set ISO to camera", exception);
                if (IsoNumber.IsSynchronousValueChange)
                    throw;
            }
            finally
            {
                try
                {
                    Camera.ResumeLiveview();
                }
                catch (Exception exception)
                {
                    IsoNumber.HaveError = true;
                    Log.Debug("Error resume live view after ISO change", exception);
                    if (IsoNumber.IsSynchronousValueChange)
                        throw;
                }
            }
        }
"""
    text = replace_once(text, old_iso, new_iso, "verified synchronous ISO write")

    old_transfer = """                    try
                    {
                        IsBusy = true;
                        Camera.PauseLiveview();
                        Log.Debug("Camera.PauseLiveview();");
                        var transporter = new EosImageTransporter();
                        transporter.ProgressEvent += (i) => TransferProgress = (uint) i;
                        Log.Debug("TransportAsFileName");
                        transporter.TransportAsFileName((IntPtr) o, filename, Camera.Handle);
                        Log.Debug("TransportAsFileName DONE");
                        Camera.ResumeLiveview();
                        Log.Debug("Camera.ResumeLiveview(); DONE");
                    }
                    catch (Exception exception)
                    {
                        Log.Error("Error transfer memory file", exception);
                        File.Delete(filename);
                    }
"""
    new_transfer = """                    try
                    {
                        IsBusy = true;
                        Camera.PauseLiveview();
                        Log.Debug("Camera.PauseLiveview();");
                        var transporter = new EosImageTransporter();
                        transporter.ProgressEvent += (i) => TransferProgress = (uint) i;
                        Log.Debug("TransportAsFileName");
                        transporter.TransportAsFileName((IntPtr) o, filename, Camera.Handle);
                        Log.Debug("TransportAsFileName DONE");
                    }
                    catch (Exception exception)
                    {
                        Log.Error("Error transfer memory file", exception);
                        File.Delete(filename);
                        throw;
                    }
                    finally
                    {
                        Camera.ResumeLiveview();
                        Log.Debug("Camera.ResumeLiveview(); DONE");
                    }
"""
    text = replace_once(text, old_transfer, new_transfer, "pointer transfer cleanup")
    path.write_text(text, encoding="utf-8")


def patch_camera_controls(source: Path) -> None:
    """Patch all camera-control sources used by the runtime build."""
    _patch_eos_camera(source)
    _patch_property_value(source)
    _patch_canon_sdk_base(source)
