<h1 align="center" style="margin-bottom:20px">Sweet-Link (Homeway.io Fork)</h1>

Sweet-Link is a custom fork of the official [Homeway.io](https://homeway.io) Home Assistant Add-on, tailored specifically for the Sweetplace ecosystem. 

It retains all the core secure tunneling and cloud synchronization features of Homeway (enabling official Alexa and Google Assistant integrations) while introducing private enterprise features for secure hardware deployments.

## 🌟 Custom Sweetplace Features

This fork introduces the following capabilities on top of the Homeway core:

- **Zero-Touch Provisioning (Hardware Claiming):** At startup, the AddOn automatically discovers the hardware's physical MAC Address and its generated Homeway Cloud URL, securely reporting them to the Sweetplace backend. This allows end-users to link their physical device with zero technical configuration.
- **Granular Entity Filtering (YAML):** Bypasses the standard Home Assistant UI toggle system. The AddOn strictly enforces exposure rules based on local YAML configuration files (`alexa.yaml`, `siri.yaml`, `homekit.yaml`), ensuring only whitelisted entities ever leave the local network.

## 🤝 Upstream Features (Homeway.io)

This project is proudly built on the shoulders of the [Homeway.io](https://homeway.io) open-source project. It inherits:
- Free remote access to Home Assistant
- Native Alexa and Google Assistant cloud integrations
- Fast WebRTC camera streaming

*Note: For official Homeway support, please visit the official Homeway community. This custom fork is maintained privately for the Sweetplace system and is not supported by the original Homeway developers.*

## 📜 License
This project is licensed under the AGPLv3 License, in compliance with the original upstream Homeway repository.