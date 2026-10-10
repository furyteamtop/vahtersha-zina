import unittest

from zina import captcha
from zina.spam import is_spam


class SpamTest(unittest.TestCase):
    def test_catches_spam(self):
        for text in [
            "Горячие фото в моём профиле 🔥",
            "пoрнo бесплатно",  # латинские «o»
            "п о р н о тут",
            "𝐩𝐨𝐫𝐧 videos",  # «жирные» юникод-буквы
            "Ссылка в профиле, заходи",
            "Пиши мне в лс, покажу",
            "Hot girls near you",
            "Сливы фото одноклассниц",
            "Заработок от 5000 в день без вложений",
            "OnlyFans бесплатно",
            "пор​но",  # невидимый символ
        ]:
            self.assertTrue(is_spam(text), text)

    def test_ignores_normal_comments(self):
        for text in [
            "Отличный пост, спасибо!",
            "Напор нормальный, а вот трубы старые",
            "Подпишусь на рассылку",
            "Я из Москвы, а вы?",
            "Классный эскиз",
            "Сколько стоит доставка?",
            "Посмотрел профиль компании на сайте",
            "",
        ]:
            self.assertFalse(is_spam(text), text)

    def test_custom_words(self):
        self.assertTrue(is_spam("Лучшее казино города", ["казино"]))
        self.assertTrue(is_spam("Лучшее кaзинo города", ["казино"]))  # латиница
        self.assertFalse(is_spam("Лучшее кино города", ["казино"]))


class CaptchaTest(unittest.TestCase):
    def test_challenges_are_valid(self):
        for _ in range(500):
            c = captcha.generate()
            self.assertTrue(0 <= c.correct < len(c.options))
            self.assertEqual(len(set(c.options)), len(c.options))


if __name__ == "__main__":
    unittest.main()
