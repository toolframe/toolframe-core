# Distribution-owned authoring

Core discovers plugin source templates from a versioned static manifest in each
distribution's wheel metadata. Core's basic template uses the same interface.
Web and Dashboard own their Notes source, generators, assets and licenses.

Static discovery can explain parameters without importing a plugin, constructing
a host or running package code. Distribution metadata supplies identity/version;
the manifest supplies authoring contracts. Runtime entry points and enabled
providers do not determine which authoring capabilities exist. Discovery validates
metadata structure and requirement syntax, and reports Core/Python compatibility
without rejecting well-formed incompatible distributions. Execution validates the
selected distribution before preparing its dependency environment or output.

Generation executes only the selected wheel in a separate dependency environment,
then publishes into an exclusively created directory. Installed RECORD files can
reconstruct a wheel when the original archive was not retained. Source-based
editable installs require an explicit wheel, so discovery never needs a build
backend or source pyproject. This adds environment preparation to generation but
keeps optional dependencies out of Core and makes the selected version explicit.

The basic default provides a small provider-free service. Presentation and storage
examples require explicit template selection. Compatibility presentation aliases
resolve through the same distribution metadata. A single source-preparation entry
supports packaged asset builds; it does not create a general command extension
framework.

An offline official directory helps discover packages without scanning an index
or installing them. Summaries are checked against matching wheels; the selected
wheel remains authoritative for parameters and executable entries. Publication
availability is separate from local matching-wheel validation.

Generated source is user-owned and is never an upgrade target. Template and guide
updates preserve it, including customized files and licenses.

See [distribution authoring](../authoring.md) for the protocol and commands.
