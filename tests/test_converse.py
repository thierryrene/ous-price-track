from __future__ import annotations

import unittest

from ous_monitor.scrapers.converse import _extract_page


HTML = """
<p class="toolbar-amount"><span class="toolbar-number">216</span> Resultados</p>
<ol class="products list items product-items">
  <li class="item product product-item" id="prod-list-153369">
    <a class="product-item-link" href="https://converse.test/produto">
      Chuck Taylor Marrom
    </a>
    <img class="product-image-photo" data-src="https://img.test/a.jpg">
    <span data-price-amount="239.9" data-price-type="finalPrice"></span>
    <span data-price-amount="349.9" data-price-type="oldPrice"></span>
    <div class="category gender_shoe-style">Unisex Cano Baixo</div>
    <form data-product-sku="CT30360002"></form>
  </li>
</ol>
<script type="text/x-magento-init">
{
  "[data-role=swatch-option-153369]": {
    "Magento_Swatches/js/swatch-renderer": {
      "childColorIdPlp": "red",
      "jsonConfig": {
        "productId": "153369",
        "attributes": {
          "278": {
            "code": "color",
            "options": [{"id": "red", "label": "Vermelho"}]
          },
          "551": {
            "code": "size",
            "options": [
              {"id": "s42", "label": "42"},
              {"id": "s43", "label": "43"}
            ]
          }
        },
        "index": {
          "v1": {"278": "red", "551": "s42"},
          "v2": {"278": "red", "551": "s43"}
        },
        "salable": {
          "278": {"red": ["v1", "v2"]},
          "551": {"s42": ["v1"], "s43": ["v2"]}
        }
      }
    }
  }
}
</script>
"""


class ConverseParserTests(unittest.TestCase):
    def test_extracts_card_prices_and_available_sizes(self):
        total, products = _extract_page(HTML)

        self.assertEqual(total, 216)
        self.assertEqual(len(products), 1)
        product = products[0]
        self.assertEqual(product.source, "converse")
        self.assertEqual(product.sku, "CT30360002")
        self.assertEqual(product.price, 239.9)
        self.assertEqual(product.list_price, 349.9)
        self.assertEqual(product.sizes, ["42", "43"])
        self.assertIn("Tênis Converse", product.name)
        self.assertIn("Unisex", product.name)


if __name__ == "__main__":
    unittest.main()
