from django.test import TestCase

from products.models import DEFAULT_HOMEPAGE_TAGLINE, HomepageBanner, HomepageSettings


class HomepageModelTests(TestCase):
    def test_default_tagline_and_order(self):
        self.assertEqual(HomepageSettings().tagline, DEFAULT_HOMEPAGE_TAGLINE)
        self.assertEqual(DEFAULT_HOMEPAGE_TAGLINE, "Deus primeiro. Negócios depois.")
        first = HomepageBanner.objects.create(image_desktop="one.png", alt_text="Um", display_order=2)
        second = HomepageBanner.objects.create(image_desktop="two.png", alt_text="Dois", display_order=1)
        self.assertEqual(list(HomepageBanner.objects.values_list("id", flat=True)), [second.id, first.id])
