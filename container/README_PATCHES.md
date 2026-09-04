# Compatibility patches included in the runtime

These are runtime dependency compatibility patches only. They do not modify
AutoCF scientific source files.

1. HydroMT-SFINCS 1.0.3:
   fix an invalid Pandas Index-vs-set truth-value comparison in
   `set_forcing_1d`.

2. HydroMT 0.9.4:
   coerce GeoPandas geometry extension arrays to 1-D object NumPy arrays when
   constructing xarray vector coordinates. The same treatment is applied to
   GeoDataArray and GeoDataset construction.

The runtime pins Pandas 2.2.3 because HydroMT-SFINCS 1.0.3 still uses the
legacy `Index.is_integer()` / `Index.is_numeric()` APIs removed in Pandas 3.
