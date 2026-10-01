# Manual tender price export policy

The manual tender price export uses only a completed durable sourcing run and
the exact source row IDs stored by the server. It writes prices into a copy of
the confirmed source workbook; it never inserts columns or evaluates workbook
formulas. Unsupported price bases, providers, units, and ambiguous workbook
targets remain blank or fail closed. Historical 1C prices require a separate
user confirmation, are visibly marked, and carry a note that they describe a
past purchase rather than current availability.

## ETM iPRO commercial price basis

The client-supplied *ETM API Product documentation*, version 24.01.2025,
documents the Price API fields as follows: `price` is the price without VAT;
`pricewnds` is the price with VAT. Phase C V1 therefore permits an ETM price
only when the persisted provider provenance identifies `pricewnds`, alongside
the other export safety checks. `price`, `price_retail`, and `price_tarif` do
not have automatic export authority.
