# MEXA relay setup has moved

Use [Wormhole setup](MEXA_QUICK_TUNNEL.md). The relay runs inside the
flow-controller app and the analyser bridge sends measurements to its
temporary WSS URL. No third PC or separately deployed relay is needed.

The existing 28 August bridge's **Internet relay (outbound WSS)** option
works unchanged; it does not need reinstalling. New bridge builds label the
same transport **Wormhole (outbound WSS)**.

[MEXA setup](MEXA_SETUP.md) covers the Direct LAN backup, instrument validation,
measurement limits, logging and live optimiser safeguards.
