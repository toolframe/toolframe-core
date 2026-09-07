import streamlit as st
from domains.stockroom import STOCKROOM
from domains.stockroom.model.service import DeliveryError
from toolframe_dashboard import (
    DashboardDatabaseError,
    DashboardInteractionError,
    render_error_card,
)
from toolframe_idempotency import IdempotencyError

TITLE = "Deliveries and stock"
ORDER = 100


def render(context):
    with context.plugins.lease(STOCKROOM) as stockroom:
        st.header(TITLE)
        with st.form("stockroom-delivery"):
            reference = st.text_input("Delivery reference", max_chars=64)
            item = st.text_input("Item", max_chars=80)
            quantity = st.number_input("Quantity", min_value=1, value=1, step=1)
            submitted = st.form_submit_button("Receive delivery")
        if submitted:
            try:
                receipt = stockroom.receive_delivery(reference, item, quantity)
            except DeliveryError as error:
                render_error_card(
                    DashboardInteractionError(
                        "stockroom_invalid_delivery",
                        str(error),
                        recovery_hint="Correct the delivery details and submit again.",
                    )
                )
            except IdempotencyError as error:
                render_error_card(
                    DashboardInteractionError(
                        error.code, error.message, recovery_hint=error.recovery_hint
                    )
                )
            except DashboardDatabaseError as error:
                render_error_card(error)
            else:
                st.success(
                    "Delivery already recorded."
                    if receipt["replayed"]
                    else "Delivery recorded."
                )
        st.dataframe(stockroom.stock_levels(), hide_index=True, width="stretch")
