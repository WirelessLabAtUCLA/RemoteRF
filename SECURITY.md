# Security Policy

RemoteRF provides networked remote access to physical software-defined radios (SDRs) and laboratory hardware, so software security and RF safety are both critical.

## Supported Versions

Security fixes are provided for the latest PyPI release of each package and the `main` branch. Older releases are not backported.

| Package | Supported Version |
|---|---|
| `remoterf` | Latest release (`main`) |
| `remoterf-server` | Latest release (`main`) |
| `remoterf-host` | Latest release (`main`) |
| `remoterf-device-core` | Latest release (`main`) |
| `remoterf-federation-core` | Latest release (`main`) |

## Reporting a Vulnerability

Please report security vulnerabilities privately using GitHub Private Vulnerability Reporting:

- Open **[Security → Advisories → Report a vulnerability](https://github.com/WirelessLabAtUCLA/RemoteRF/security/advisories/new)** on this repository.
- **Do not** open a public GitHub issue, pull request, or discussion for a security vulnerability.

To help us triage quickly, please include:

- Affected package(s) and version(s)
- Description of the issue and its potential security or RF safety impact
- Minimal reproduction steps or a proof of concept
- Relevant deployment context (e.g., local vs. Global federation, custom schemas, OS and Python version)

We aim to acknowledge reports within 7 days, coordinate a fix and disclosure timeline with you prior to public release, and credit you in the advisory if desired.

## Scope & Safe Testing

### In Scope

- Authentication, user-group permission, or reservation-enforcement bypasses
- Bypasses of server-side RF safeguards or software lockouts (e.g., transmit power limits, frequency bounds, or schema parameter validation)
- Arbitrary code execution or server/host escape via client RPCs or device schema handling
- Exposure of credentials, reservation tokens, or enrollment codes
- Security flaws in `remoterf-server` ↔ `remoterf-host` or lab ↔ RemoteRF Global federation communication

### Out of Scope

- Misconfigurations of self-hosted deployments (such as exposed ports, weak credentials, unsafe custom schemas, or missing physical RF safeguards)
- Vulnerabilities solely within upstream vendor hardware SDKs or drivers unless introduced or amplified by RemoteRF

### Responsible Testing

> **Never test vulnerabilities against live shared deployments** (including UCLA's deployment or nodes connected to `global.remoterf.net`), and never transmit unauthorized over-the-air RF signals. Reproduce issues only against a local test instance you control, using mock devices or properly attenuated/cabled hardware.

## Operator Hardening Notes

If you operate a `remoterf-server` or `remoterf-host` deployment:

- **Combine software and physical RF safeguards:** Pair software lockouts with physical protections (inline attenuators, filters, shielding, dummy loads, or cabled links) to protect hardware and maintain regulatory compliance.
- **Restrict network exposure and privileges:** Place deployments behind appropriate firewalls, VPNs, or TLS/HTTPS, and run server and host processes with least privilege (avoid running as `root`).
- **Audit custom device schemas:** Server-side wrappers and schemas execute directly inside the server or host environment—review custom code carefully before enabling it.
- **Stay current:** Keep `remoterf-server`, `remoterf-host`, and `remoterf` updated, and review the [Server Guide](https://remoterf.net/docs/server/) and [Terms of Service](https://remoterf.net/terms/).
