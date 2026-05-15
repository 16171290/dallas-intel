from scraper import dcad_bulk
import pandas as pd

tables = dcad_bulk.parse_dcad_tables(dcad_bulk.fetch_dcad_zip())
df = tables["APPLIED_STD_EXEMPT"]

print(f"APPLIED_STD_EXEMPT has {len(df):,} rows")
print(f"Columns: {list(df.columns)}")
print()

# Top values in each *_DESC column (and HOMESTEAD_EFF_DT for comparison)
for col in ["HOMESTEAD_EFF_DT", "OVER65_DESC", "DISABLED_DESC", "TAX_DEFERRED_DESC"]:
    print(f"--- {col} ---")
    counts = df[col].astype(str).str.strip().value_counts(dropna=False).head(15)
    print(counts.to_string())
    print()

# Show the actual rows for one of the suspicious accounts
print("--- Rows for account 36059500080380000 (942 BRIARCOVE PL) ---")
sus = df[df["ACCOUNT_NUM"] == "36059500080380000"]
pd.set_option("display.max_columns", None)
pd.set_option("display.width", 200)
print(sus[["ACCOUNT_NUM", "APPRAISAL_YR", "OWNER_SEQ_NUM", "HOMESTEAD_EFF_DT", "OVER65_DESC", "DISABLED_DESC", "TAX_DEFERRED_DESC"]].to_string(index=False))
